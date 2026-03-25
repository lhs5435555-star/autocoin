"""
포지션 관리 모듈.

PositionState, PhantomTracker, BlockedSignalTracker, PositionManager
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from fang_v10.config import CONFIG

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────
# A. PositionState (개별 포지션)
# ──────────────────────────────────────────────

@dataclass
class PositionState:
    """개별 포지션 상태.

    initial_r_distance: 최초 진입 시 atr × SL_ATR_MULT[asset].
    DCA 후에도 절대 변경 안됨. 모든 R 계산의 기준.
    """

    symbol: str
    side: str                           # "long" / "short"
    entries: List[Tuple[float, float]] = field(default_factory=list)  # [(price, size), ...]
    avg_price: float = 0.0
    total_size: float = 0.0
    dca_count: int = 0                  # 최대 1
    leverage: int = 1
    atr: float = 0.0
    regime: str = ""
    strategy: str = ""

    initial_r_distance: float = 0.0     # 1R 가격 거리 (불변)

    peak_r: float = 0.0                 # calc_r() 최대값
    trough_r: float = 0.0              # calc_r() 최소값

    favorable_extreme: float = 0.0      # Long: 최고가, Short: 최저가

    be_activated: bool = False
    tp_count: int = 0
    remaining_ratio: float = 1.0
    realized_pnl: float = 0.0
    entry_bar: int = 0
    entry_time: float = 0.0

    def add_dca(self, price: float, size: float) -> None:
        """DCA 추가 진입. VWAP 평균가 재계산. initial_r_distance 불변."""
        self.entries.append((price, size))
        self.dca_count += 1

        # VWAP 재계산
        total_cost = sum(p * s for p, s in self.entries)
        self.total_size = sum(s for _, s in self.entries)
        self.avg_price = total_cost / self.total_size if self.total_size > 0 else price

    def calc_r(self, current_price: float) -> float:
        """현재 가격 기준 R 배수 계산.

        R 계산 기준:
        - 분모 (initial_r_distance): 최초 진입 시 ATR × SL_ATR_MULT. DCA 후 불변.
        - 분자 (가격 차이): avg_price 기준. DCA 후 avg_price가 바뀜.

        설계 의도:
        - "총 리스크를 초기 1R 기준으로 묶는다"
        - DCA로 avg_price가 유리해지면 같은 가격 이동에서 R값이 달라짐
        - 이는 의도된 동작: DCA 성공 시 TP 도달이 쉬워짐

        주의:
        - risk_r_basis (리스크 한도 판단) = initial_r_distance 고정
        - price_exit_basis (체결가 비교) = avg_price 변동
        - 이 두 기준이 분리되어 있음을 인지해야 함
        """
        if self.initial_r_distance <= 0:
            return 0.0
        if self.side == "long":
            return (current_price - self.avg_price) / self.initial_r_distance
        return (self.avg_price - current_price) / self.initial_r_distance

    def update_peaks(self, current_price: float) -> None:
        """R 피크/트로프 및 favorable_extreme 갱신."""
        current_r = self.calc_r(current_price)
        self.peak_r = max(self.peak_r, current_r)
        self.trough_r = min(self.trough_r, current_r)

        if self.side == "long":
            self.favorable_extreme = max(self.favorable_extreme, current_price)
        else:
            self.favorable_extreme = min(self.favorable_extreme, current_price)


# ──────────────────────────────────────────────
# B. PhantomTracker (SL 선점 추적)
# ──────────────────────────────────────────────

class PhantomTracker:
    """SL로 청산된 포지션을 유령으로 추적.

    "SL 안 잘랐으면 어땠을까?" 데이터를 수집.
    """

    def __init__(self, track_bars: int = CONFIG.SL_PHANTOM_TRACK_BARS) -> None:
        self.track_bars = track_bars
        self.phantoms: List[Dict] = []
        self.completed: List[Dict] = []

    def register_sl(
        self,
        symbol: str, side: str, entry_price: float,
        avg_price: float, sl_price: float, tp_price: float,
        sl_bar_idx: int, r_at_sl: float, pnl_at_sl: float,
    ) -> None:
        """SL 청산된 포지션을 유령으로 등록."""
        self.phantoms.append({
            "symbol": symbol, "side": side,
            "entry_price": entry_price, "avg_price": avg_price,
            "sl_price": sl_price, "tp_price": tp_price,
            "sl_bar_idx": sl_bar_idx,
            "r_at_sl": r_at_sl, "pnl_at_sl": pnl_at_sl,
            "bars_tracked": 0,
            "peak_favorable": 0.0,
            "peak_adverse": 0.0,
            "would_have_hit_tp": False,
            "would_have_hit_deeper_sl": False,
        })

    def update(self, symbol: str, current_price: float, bar_idx: int) -> None:
        """매 봉마다 유령 포지션 업데이트."""
        remaining = []
        for p in self.phantoms:
            if p["symbol"] != symbol:
                remaining.append(p)
                continue

            p["bars_tracked"] = bar_idx - p["sl_bar_idx"]

            # 유리/불리 방향 이동 계산
            avg = p["avg_price"]
            if p["side"] == "long":
                favorable_move = (current_price - avg) / avg if avg > 0 else 0
                adverse_move = (avg - current_price) / avg if avg > 0 else 0
            else:
                favorable_move = (avg - current_price) / avg if avg > 0 else 0
                adverse_move = (current_price - avg) / avg if avg > 0 else 0

            p["peak_favorable"] = max(p["peak_favorable"], favorable_move)
            p["peak_adverse"] = max(p["peak_adverse"], adverse_move)

            # TP 도달 여부
            if p["side"] == "long" and current_price >= p["tp_price"]:
                p["would_have_hit_tp"] = True
            elif p["side"] == "short" and current_price <= p["tp_price"]:
                p["would_have_hit_tp"] = True

            # 더 깊은 손실 (SL 2배 거리)
            sl_dist = abs(p["entry_price"] - p["sl_price"])
            if p["side"] == "long" and current_price <= p["entry_price"] - sl_dist * 2:
                p["would_have_hit_deeper_sl"] = True
            elif p["side"] == "short" and current_price >= p["entry_price"] + sl_dist * 2:
                p["would_have_hit_deeper_sl"] = True

            # 추적 완료 체크
            if p["bars_tracked"] >= self.track_bars:
                self.completed.append(p)
            else:
                remaining.append(p)

        self.phantoms = remaining

    def get_stats(self) -> Dict:
        """유령 추적 통계."""
        total = len(self.completed)
        if total == 0:
            return {"total": 0, "premature_count": 0, "premature_pct": 0,
                    "correct_count": 0, "correct_pct": 0,
                    "avg_peak_favorable_pct": 0, "avg_peak_adverse_pct": 0}

        premature = sum(1 for p in self.completed if p["would_have_hit_tp"])
        correct = sum(1 for p in self.completed if p["would_have_hit_deeper_sl"])

        avg_fav = sum(p["peak_favorable"] for p in self.completed) / total * 100
        avg_adv = sum(p["peak_adverse"] for p in self.completed) / total * 100

        return {
            "total": total,
            "premature_count": premature,
            "premature_pct": round(premature / total * 100, 1),
            "correct_count": correct,
            "correct_pct": round(correct / total * 100, 1),
            "avg_peak_favorable_pct": round(avg_fav, 2),
            "avg_peak_adverse_pct": round(avg_adv, 2),
        }

    def get_recommendation(self) -> str:
        """SL 배수 조정 권장."""
        stats = self.get_stats()
        if stats["total"] < 20:
            return "데이터 부족"
        if stats["premature_pct"] >= 40:
            return "SL 배수 20~30% 확대 권장"
        if stats["correct_pct"] >= 60:
            return "현재 SL 배수 적절"
        return "현재 SL 배수 유지"


# ──────────────────────────────────────────────
# C. BlockedSignalTracker (차단 반사실 추적)
# ──────────────────────────────────────────────

class BlockedSignalTracker:
    """차단된 시그널의 "만약 진입했으면?" 추적."""

    def __init__(self, track_bars: int = CONFIG.BLOCKED_SIGNAL_TRACK_BARS) -> None:
        self.track_bars = track_bars
        self.blocked: List[Dict] = []
        self.completed: List[Dict] = []

    def register_blocked(
        self,
        symbol: str, side: str, entry_price: float,
        sl_price: float, tp_price: float,
        block_reason: str, bar_idx: int,
    ) -> None:
        """차단된 시그널 등록."""
        self.blocked.append({
            "symbol": symbol, "side": side,
            "entry_price": entry_price,
            "sl_price": sl_price, "tp_price": tp_price,
            "block_reason": block_reason, "bar_idx": bar_idx,
            "bars_tracked": 0,
            "would_have_hit_tp": False,
            "would_have_hit_sl": False,
            "peak_favorable_pct": 0.0,
            "peak_adverse_pct": 0.0,
        })

    def update(self, symbol: str, current_price: float, bar_idx: int) -> None:
        """매 봉마다 차단 시그널 업데이트."""
        remaining = []
        for b in self.blocked:
            if b["symbol"] != symbol:
                remaining.append(b)
                continue

            b["bars_tracked"] = bar_idx - b["bar_idx"]
            entry = b["entry_price"]

            # 유리/불리 방향
            if b["side"] == "long":
                fav = (current_price - entry) / entry if entry > 0 else 0
                adv = (entry - current_price) / entry if entry > 0 else 0
            else:
                fav = (entry - current_price) / entry if entry > 0 else 0
                adv = (current_price - entry) / entry if entry > 0 else 0

            b["peak_favorable_pct"] = max(b["peak_favorable_pct"], fav * 100)
            b["peak_adverse_pct"] = max(b["peak_adverse_pct"], adv * 100)

            # TP/SL 도달
            if b["side"] == "long":
                if current_price >= b["tp_price"]:
                    b["would_have_hit_tp"] = True
                if current_price <= b["sl_price"]:
                    b["would_have_hit_sl"] = True
            else:
                if current_price <= b["tp_price"]:
                    b["would_have_hit_tp"] = True
                if current_price >= b["sl_price"]:
                    b["would_have_hit_sl"] = True

            if b["bars_tracked"] >= self.track_bars:
                self.completed.append(b)
            else:
                remaining.append(b)

        self.blocked = remaining

    def get_stats(self) -> Dict:
        """차단 시그널 통계."""
        total = len(self.completed)
        if total == 0:
            return {"total": 0, "correct_block_count": 0, "correct_block_pct": 0,
                    "wrong_block_count": 0, "wrong_block_pct": 0}

        correct = sum(1 for b in self.completed if b["would_have_hit_sl"])
        wrong = sum(1 for b in self.completed if b["would_have_hit_tp"])

        return {
            "total": total,
            "correct_block_count": correct,
            "correct_block_pct": round(correct / total * 100, 1),
            "wrong_block_count": wrong,
            "wrong_block_pct": round(wrong / total * 100, 1),
        }


# ──────────────────────────────────────────────
# D. PositionManager (메인 매니저)
# ──────────────────────────────────────────────

class PositionManager:
    """포지션 생명주기 관리.

    open, DCA, exit (SL/TP/BE/TRAIL/EARLY/TIME/TREND_REV/EMERGENCY).
    """

    def __init__(self) -> None:
        self.positions: Dict[str, PositionState] = {}  # key = "{symbol}|{side}"
        self.phantom = PhantomTracker()
        self.blocked = BlockedSignalTracker()

    def open_position(
        self,
        symbol: str, side: str, entry_price: float, size: float,
        leverage: int, atr: float, bar_idx: int,
        regime: str, strategy: str,
    ) -> PositionState:
        """새 포지션 생성."""
        asset = "BTC" if "BTC" in symbol else "ETH"
        initial_r_distance = atr * CONFIG.SL_ATR_MULT.get(asset, 1.5)

        pos = PositionState(
            symbol=symbol, side=side,
            entries=[(entry_price, size)],
            avg_price=entry_price,
            total_size=size,
            leverage=leverage, atr=atr,
            regime=regime, strategy=strategy,
            initial_r_distance=initial_r_distance,
            favorable_extreme=entry_price,
            entry_bar=bar_idx,
            entry_time=time.time(),
        )

        key = f"{symbol}|{side}"
        self.positions[key] = pos
        logger.info(
            "포지션 오픈: %s %s @ %.2f, 1R=%.2f USDT",
            symbol, side, entry_price, initial_r_distance,
        )
        return pos

    def check_dca(
        self,
        symbol: str, side: str, current_price: float, atr: float,
        bar_idx: int, ema9: Optional[float] = None, ema21: Optional[float] = None,
    ) -> Optional[Dict]:
        """DCA 진입 조건 확인.

        조건 (전부 AND):
          - 역행 거리 >= ATR × DCA_ATR_MULT
          - dca_count == 0 (1회 제한)
          - EMA9/21 역전 아님

        Returns:
            {"price": float, "size_ratio": float} 또는 None
        """
        key = f"{symbol}|{side}"
        pos = self.positions.get(key)
        if pos is None:
            return None

        # 1회 제한
        if pos.dca_count >= CONFIG.DCA_MAX:
            return None

        # 역행 거리 계산
        if side == "long":
            adverse_dist = pos.avg_price - current_price
        else:
            adverse_dist = current_price - pos.avg_price

        dca_threshold = atr * CONFIG.DCA_ATR_MULT
        if adverse_dist < dca_threshold:
            return None

        # EMA 역전 체크 (추세 역전 중이면 DCA 금지)
        if ema9 is not None and ema21 is not None:
            if side == "long" and ema9 < ema21:
                return None
            if side == "short" and ema9 > ema21:
                return None

        return {"price": current_price, "size_ratio": CONFIG.DCA_SIZE_RATIO}

    def check_exit(
        self,
        key: str, current_price: float, bar_idx: int,
        ema9: Optional[float] = None, ema21: Optional[float] = None,
        df=None,
    ) -> Optional[Dict]:
        """포지션 청산 조건 확인.

        우선순위:
          1. 비상 SL (-3.0R)
          2. 조기실패컷 (8봉, peak_r<0.5R)
          3. 추세역전 (TREND 전략만)
          4. 시간초과
          5. ATR SL (-1.0R)
          6. BE 보호 (+0.5R)
          7. TP1 (+1.0R)
          8. TP2 (+1.5R)
          9. TP3 트레일

        Returns:
            {"action": str, "ratio": float, "reason": str, ...} 또는 None
        """
        pos = self.positions.get(key)
        if pos is None:
            return None

        current_r = pos.calc_r(current_price)
        pos.update_peaks(current_price)
        hold_bars = bar_idx - pos.entry_bar

        asset = "BTC" if "BTC" in pos.symbol else "ETH"

        # ── 1. 비상 SL ──
        if current_r <= -3.0:
            return {"action": "close", "ratio": 1.0, "reason": "EMERGENCY"}

        # ── 2. 조기실패컷 ──
        if (hold_bars >= CONFIG.EARLY_CUT_BARS
                and pos.dca_count == 0
                and pos.peak_r < CONFIG.EARLY_CUT_PEAK_R):
            return {"action": "close", "ratio": 1.0, "reason": "EARLY"}

        # ── 3. 추세역전 (TREND 전략만) ──
        if (pos.strategy == "trend"
                and ema9 is not None and ema21 is not None):
            if pos.side == "long" and ema9 < ema21:
                return {"action": "close", "ratio": 1.0, "reason": "TREND_REV"}
            if pos.side == "short" and ema9 > ema21:
                return {"action": "close", "ratio": 1.0, "reason": "TREND_REV"}

        # ── 4. 시간초과 ──
        if hold_bars >= CONFIG.MAX_HOLD_BARS and current_r < 0:
            return {"action": "close", "ratio": 1.0, "reason": "TIME"}

        # ── 5. ATR SL ──
        if current_r <= -1.0:
            # 유령 추적 등록
            sl_price = (pos.avg_price - pos.initial_r_distance
                        if pos.side == "long"
                        else pos.avg_price + pos.initial_r_distance)
            tp_price = (pos.avg_price + pos.initial_r_distance * CONFIG.TP1_R
                        if pos.side == "long"
                        else pos.avg_price - pos.initial_r_distance * CONFIG.TP1_R)
            self.phantom.register_sl(
                symbol=pos.symbol, side=pos.side,
                entry_price=pos.entries[0][0],
                avg_price=pos.avg_price,
                sl_price=sl_price, tp_price=tp_price,
                sl_bar_idx=bar_idx,
                r_at_sl=current_r,
                pnl_at_sl=pos.realized_pnl,
            )
            return {"action": "close", "ratio": 1.0, "reason": "SL"}

        # ── 6. BE 보호 ──
        if current_r >= CONFIG.BE_TRIGGER_R and not pos.be_activated:
            # 펀딩비 포함 BE 가격 계산
            hold_hours = (time.time() - pos.entry_time) / 3600
            funding_cost_pct = (hold_hours // 8) * CONFIG.FUNDING_RATE_PER_8H
            slippage = CONFIG.SLIPPAGE.get(asset, 0.0003)
            round_trip_cost_pct = CONFIG.EFFECTIVE_TAKER_FEE * 2 + slippage
            total_cost_pct = round_trip_cost_pct + funding_cost_pct

            if pos.side == "long":
                be_price = pos.avg_price * (1 + total_cost_pct)
            else:
                be_price = pos.avg_price * (1 - total_cost_pct)

            pos.be_activated = True
            return {"action": "move_sl", "ratio": 0.0,
                    "reason": "BE", "new_sl": round(be_price, 8)}

        # ── 7. TP1 ──
        if current_r >= CONFIG.TP1_R and pos.tp_count == 0:
            return {"action": "partial", "ratio": CONFIG.TP1_RATIO, "reason": "TP1"}

        # ── 8. TP2 ──
        if current_r >= CONFIG.TP2_R and pos.tp_count == 1:
            # 잔량의 60% = 전체의 약 30%
            return {"action": "partial", "ratio": 0.60, "reason": "TP2"}

        # ── 9. TP3 트레일 ──
        if pos.tp_count >= 2:
            trail_distance = pos.initial_r_distance * CONFIG.TP3_TRAIL_R
            if pos.side == "long":
                trail_stop = pos.favorable_extreme - trail_distance
                if current_price <= trail_stop:
                    return {"action": "close", "ratio": 1.0, "reason": "TRAIL"}
            else:
                trail_stop = pos.favorable_extreme + trail_distance
                if current_price >= trail_stop:
                    return {"action": "close", "ratio": 1.0, "reason": "TRAIL"}

        return None
