"""
포지션 사이징 엔진.

1R 리스크 기반 포지션 크기 계산, 레버리지 자동 결정,
6단계 검증 게이트, DCA 리스크 관리.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Dict, List, Optional

from fang_v10.config import CONFIG

logger = logging.getLogger(__name__)

# 최소 주문 수량
MIN_ORDER_AMOUNT: Dict[str, float] = {"BTC": 0.001, "ETH": 0.01}


@dataclass
class SizingResult:
    """포지션 사이징 결과."""
    symbol: str
    side: str
    entry_price: float
    sl_price: float
    tp_price: float
    notional: float          # 명목가치 (USDT)
    margin: float            # 필요 마진 (USDT)
    amount: float            # 주문 수량 (코인)
    leverage: int            # 레버리지 배수
    risk_1r_usd: float       # 1R 리스크 금액 (USDT)
    sl_distance_pct: float   # SL 거리 (%)
    tp_distance_pct: float   # TP 거리 (%)
    liq_distance_pct: float  # 청산 거리 (%)
    gross_rr: float          # 비용 제외 RR 비율
    net_rr: float            # 비용 포함 순 RR 비율
    total_cost_pct: float    # 총 비용 비율 (%)
    valid: bool              # 검증 통과 여부
    reject_reason: str       # 거절 사유 (valid=True이면 빈 문자열)


def calc_position_size(
    balance: float,
    entry: float,
    sl: float,
    tp: float,
    symbol: str,
    kelly_mult: float = 1.0,
) -> SizingResult:
    """1R 리스크 기반 포지션 크기 계산.

    Args:
        balance: 현재 잔고 (USDT)
        entry: 진입 가격 (USDT)
        sl: 손절 가격 (USDT)
        tp: 익절 가격 (USDT)
        symbol: 심볼 (예: "BTC/USDT:USDT")
        kelly_mult: 켈리 배수 (0.3~1.5 클램핑)

    Returns:
        SizingResult (valid=False이면 진입 금지)
    """
    asset = _get_asset(symbol)
    side = "long" if sl < entry else "short"

    # 기본 reject 결과 템플릿
    def _reject(reason: str) -> SizingResult:
        return SizingResult(
            symbol=symbol, side=side, entry_price=entry,
            sl_price=sl, tp_price=tp,
            notional=0, margin=0, amount=0, leverage=0,
            risk_1r_usd=0, sl_distance_pct=0, tp_distance_pct=0,
            liq_distance_pct=0, gross_rr=0, net_rr=0, total_cost_pct=0,
            valid=False, reject_reason=reason,
        )

    # ── 켈리 클램핑 ──
    kelly_clamped = max(0.3, min(1.5, kelly_mult))
    if kelly_mult < 0.3:
        return _reject(f"kelly_mult={kelly_mult:.2f} < 0.3 → 진입 금지")

    # ── SL/TP 거리 ──
    sl_dist = abs(entry - sl) / entry
    tp_dist = abs(tp - entry) / entry

    # ── 비용 계산 ──
    slippage = CONFIG.SLIPPAGE.get(asset, 0.0003)
    cost = CONFIG.EFFECTIVE_TAKER_FEE * 2 + slippage
    total_cost_pct = cost

    # ── 1R 리스크 금액 ──
    risk_1r_usd = balance * CONFIG.RISK_PER_TRADE_PCT / 100 * kelly_clamped

    # ── 명목가치 ──
    if sl_dist + cost <= 0:
        return _reject("SL 거리 + 비용 <= 0")
    notional = risk_1r_usd / (sl_dist + cost)

    # ── 레버리지 자동 결정 (청산거리 = SL×2 확보) ──
    leverage = _auto_leverage(sl_dist, asset)

    # ── 마진, 수량 ──
    margin = notional / leverage
    amount = notional / entry

    # ── 청산 거리 ──
    liq_dist_pct = 1.0 / leverage  # 단순 근사: 100% / leverage

    # ── RR 비율 ──
    gross_rr = tp_dist / sl_dist if sl_dist > 0 else 0
    net_rr = (tp_dist - cost) / (sl_dist + cost) if (sl_dist + cost) > 0 else 0

    result = SizingResult(
        symbol=symbol, side=side, entry_price=entry,
        sl_price=sl, tp_price=tp,
        notional=round(notional, 2),
        margin=round(margin, 2),
        amount=_round_amount(amount, asset),
        leverage=leverage,
        risk_1r_usd=round(risk_1r_usd, 2),
        sl_distance_pct=round(sl_dist, 6),
        tp_distance_pct=round(tp_dist, 6),
        liq_distance_pct=round(liq_dist_pct, 6),
        gross_rr=round(gross_rr, 3),
        net_rr=round(net_rr, 3),
        total_cost_pct=round(total_cost_pct, 6),
        valid=True, reject_reason="",
    )

    # ── 6단계 검증 게이트 ──
    reject = _validate_gates(result, balance, asset)
    if reject:
        result.valid = False
        result.reject_reason = reject
        logger.info("사이징 거절 [%s]: %s", symbol, reject)

    return result


def check_dca_risk_gate(
    symbol: str,
    dca_margin: float,
    balance: float,
    positions: List[Dict],
) -> tuple[bool, str]:
    """DCA 추가 진입 리스크 게이트.

    검증:
      - 총마진/balance <= 10%
      - 심볼마진/balance <= 8%
      - 총리스크 <= 초기R × 1.5

    Args:
        symbol: DCA 대상 심볼
        dca_margin: DCA 추가 마진 (USDT)
        balance: 현재 잔고 (USDT)
        positions: 기존 포지션 리스트 [{symbol, margin, initial_risk_1r_usd, ...}]

    Returns:
        (allowed: bool, reason: str)
    """
    if balance <= 0:
        return False, "잔고 0 이하"

    total_margin = sum(p.get("margin", 0) for p in positions) + dca_margin
    symbol_margin = sum(
        p.get("margin", 0) for p in positions if p.get("symbol") == symbol
    ) + dca_margin

    # 총마진 / balance <= 10%
    if total_margin / balance > 0.10:
        return False, f"총마진비율 {total_margin / balance:.1%} > 10%"

    # 심볼마진 / balance <= 8%
    if symbol_margin / balance > 0.08:
        return False, f"심볼마진비율 {symbol_margin / balance:.1%} > 8%"

    # 총리스크 <= 초기R × 1.5
    symbol_positions = [p for p in positions if p.get("symbol") == symbol]
    if symbol_positions:
        initial_r = symbol_positions[0].get("initial_risk_1r_usd", 0)
        if initial_r > 0:
            current_total_risk = sum(
                p.get("initial_risk_1r_usd", 0) for p in symbol_positions
            )
            # DCA 추가분 추정: DCA 마진 기준 리스크
            dca_risk_est = initial_r * CONFIG.DCA_SIZE_RATIO
            projected_risk = current_total_risk + dca_risk_est
            if projected_risk > initial_r * 1.5:
                return False, (
                    f"총리스크 {projected_risk:.2f} USDT > "
                    f"초기R×1.5 ({initial_r * 1.5:.2f} USDT)"
                )

    return True, "DCA 리스크 통과"


# ──────────────────────────────────────────────
# 내부 헬퍼
# ──────────────────────────────────────────────

def _auto_leverage(sl_dist: float, asset: str) -> int:
    """레버리지 자동 결정. 청산거리 = SL×2 확보.

    Args:
        sl_dist: SL 거리 비율 (예: 0.01 = 1%)
        asset: "BTC" / "ETH"

    Returns:
        레버리지 배수 (정수)
    """
    max_lev = CONFIG.LEVERAGE_MAX.get(asset, 20)
    min_lev = CONFIG.LEVERAGE_MIN

    if sl_dist <= 0:
        return min_lev

    # 청산거리 = 1/leverage >= sl_dist * 2
    # leverage <= 1 / (sl_dist * 2)
    target_lev = int(1 / (sl_dist * 2))
    leverage = max(min_lev, min(max_lev, target_lev))
    return leverage


def _validate_gates(result: SizingResult, balance: float, asset: str) -> str:
    """6단계 검증 게이트. 실패 시 사유 문자열 반환, 통과 시 빈 문자열.

    1. SL_DIST_MIN < sl_dist < 5%
    2. margin <= balance × 10%
    3. sl_dist / liq_dist < 70%
    4. LEVERAGE_MIN <= leverage <= LEVERAGE_MAX[asset]
    5. net_rr >= 0.8
    6. amount >= 최소주문
    """
    # 1. SL 거리 범위
    if result.sl_distance_pct < CONFIG.SL_DIST_MIN:
        return (f"SL 거리 {result.sl_distance_pct:.4%} < "
                f"최소 {CONFIG.SL_DIST_MIN:.4%}")
    if result.sl_distance_pct >= 0.05:
        return f"SL 거리 {result.sl_distance_pct:.4%} >= 5%"

    # 2. 마진 한도
    if balance > 0 and result.margin > balance * 0.10:
        return (f"마진 {result.margin:.2f} USDT > "
                f"잔고의 10% ({balance * 0.10:.2f} USDT)")

    # 3. SL/청산 비율
    if result.liq_distance_pct > 0:
        sl_liq_ratio = result.sl_distance_pct / result.liq_distance_pct
        if sl_liq_ratio >= 0.70:
            return f"SL/청산 비율 {sl_liq_ratio:.2%} >= 70%"

    # 4. 레버리지 범위
    max_lev = CONFIG.LEVERAGE_MAX.get(asset, 20)
    if result.leverage < CONFIG.LEVERAGE_MIN:
        return f"레버리지 {result.leverage}x < 최소 {CONFIG.LEVERAGE_MIN}x"
    if result.leverage > max_lev:
        return f"레버리지 {result.leverage}x > 최대 {max_lev}x"

    # 5. 순 RR
    if result.net_rr < 0.8:
        return f"순 RR {result.net_rr:.3f} < 0.8"

    # 6. 최소 주문 수량
    min_amount = MIN_ORDER_AMOUNT.get(asset, 0.001)
    if result.amount < min_amount:
        return (f"수량 {result.amount} < "
                f"최소 {min_amount} {asset}")

    return ""


def _round_amount(amount: float, asset: str) -> float:
    """자산별 수량 반올림."""
    if asset == "BTC":
        return round(amount, 3)  # 0.001 단위
    return round(amount, 2)  # 0.01 단위


def _get_asset(symbol: str) -> str:
    """심볼에서 자산명 추출."""
    if "BTC" in symbol.upper():
        return "BTC"
    return "ETH"
