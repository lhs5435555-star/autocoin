"""
매매 전략 모듈 — TREND / BOX 두 가지만.

PROTECT 레짐에서는 진입 절대 금지 (빈 리스트 반환).
SL/TP는 ATR R 기반으로 계산.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import List, Optional

import pandas as pd

from fang_v10.config import CONFIG
from fang_v10.regime_engine import MarketRegime, ensure_indicators

logger = logging.getLogger(__name__)


@dataclass
class Signal:
    """매매 신호."""
    symbol: str
    side: str               # "long" / "short"
    strategy: str            # "trend" / "box"
    strength: float          # 0~1
    entry_price: float
    sl_price: float
    tp_price: float
    reason: str
    regime: MarketRegime


def generate_signals(
    symbol: str,
    df: pd.DataFrame,
    regime: MarketRegime,
    last_entry_candle: Optional[int] = None,
) -> List[Signal]:
    """현재 봉 기준 매매 신호 생성.

    Args:
        symbol: 심볼 (예: "BTC/USDT:USDT")
        df: OHLCV DataFrame (ensure_indicators 미적용 가능)
        regime: 현재 시장 레짐
        last_entry_candle: 마지막 진입 봉 인덱스 (중복 진입 방지)

    Returns:
        Signal 리스트 (PROTECT이면 빈 리스트)
    """
    # Edge case: df 없거나 비어있음
    if df is None or df.empty:
        return []

    # PROTECT → 진입 절대 금지
    if regime == MarketRegime.PROTECT:
        return []

    df = ensure_indicators(df)

    if len(df) < 50:
        logger.warning("%s 데이터 부족 (%d봉) → 신호 없음", symbol, len(df))
        return []

    bar_idx = len(df) - 1
    row = df.iloc[bar_idx]

    # ATR=0 → 0으로 나누기 방지
    if float(row.get("atr", 0)) <= 0:
        return []

    # prev_close 접근 시 최소 2봉 필요
    if len(df) < 2:
        return []

    # 중복 진입 방지
    if last_entry_candle is not None and bar_idx == last_entry_candle:
        return []

    asset = _get_asset(symbol)
    signals: List[Signal] = []

    if regime == MarketRegime.TREND:
        sig = _check_trend(symbol, asset, row, bar_idx)
        if sig:
            signals.append(sig)
    elif regime == MarketRegime.BOX:
        sig = _check_box(symbol, asset, df, bar_idx)
        if sig:
            signals.append(sig)

    return signals


# ──────────────────────────────────────────────
# TREND 전략
# ──────────────────────────────────────────────

def _check_trend(
    symbol: str, asset: str, row: pd.Series, bar_idx: int
) -> Optional[Signal]:
    """TREND 롱/숏 조건 확인.

    롱 (AND):
      adx>=20, ema9>ema21, close>ema21,
      40<rsi<80, volume_ratio>=0.8, (close-ema21)/atr<3.0

    숏: 대칭
    """
    adx = row.get("adx", 0)
    ema9 = row.get("ema9", 0)
    ema21 = row.get("ema21", 0)
    close = row.get("close", 0)
    rsi = row.get("rsi", 50)
    volume_ratio = row.get("volume_ratio", 0)
    atr = row.get("atr", 0)

    if adx < 20 or volume_ratio < 0.8 or atr <= 0:
        return None

    # ── 롱 ──
    if (ema9 > ema21
            and close > ema21
            and 40 < rsi < 80):
        ema_dist = (close - ema21) / atr if atr > 0 else 999
        if ema_dist < 3.0:
            entry = close
            sl, tp = _calc_sl_tp(entry, "long", atr, asset)
            strength = _trend_strength(adx, volume_ratio, rsi)
            return Signal(
                symbol=symbol, side="long", strategy="trend",
                strength=strength, entry_price=entry,
                sl_price=sl, tp_price=tp,
                reason=f"TREND LONG: ADX={adx:.1f}, RSI={rsi:.1f}, VR={volume_ratio:.2f}",
                regime=MarketRegime.TREND,
            )

    # ── 숏 (대칭) ──
    if (ema9 < ema21
            and close < ema21
            and 20 < rsi < 60):
        ema_dist = (ema21 - close) / atr if atr > 0 else 999
        if ema_dist < 3.0:
            entry = close
            sl, tp = _calc_sl_tp(entry, "short", atr, asset)
            strength = _trend_strength(adx, volume_ratio, 100 - rsi)
            return Signal(
                symbol=symbol, side="short", strategy="trend",
                strength=strength, entry_price=entry,
                sl_price=sl, tp_price=tp,
                reason=f"TREND SHORT: ADX={adx:.1f}, RSI={rsi:.1f}, VR={volume_ratio:.2f}",
                regime=MarketRegime.TREND,
            )

    return None


def _trend_strength(adx: float, volume_ratio: float, rsi_score: float) -> float:
    """TREND 신호 강도 0~1 계산."""
    adx_s = min((adx - 20) / 30, 1.0) if adx >= 20 else 0
    vr_s = min((volume_ratio - 0.8) / 2.2, 1.0) if volume_ratio >= 0.8 else 0
    rsi_s = min((rsi_score - 40) / 35, 1.0) if rsi_score >= 40 else 0
    return round(max(0.0, min(1.0, adx_s * 0.4 + vr_s * 0.3 + rsi_s * 0.3)), 3)


# ──────────────────────────────────────────────
# BOX 전략
# ──────────────────────────────────────────────

def _check_box(
    symbol: str, asset: str, df: pd.DataFrame, bar_idx: int
) -> Optional[Signal]:
    """BOX 반등확인형 롱/숏 조건 확인.

    롱 (7개 전부 AND):
      prev_close < bb_lower, close > bb_lower (반등 확인),
      rsi > prev_rsi (RSI 상향), rsi <= 40,
      volume_ratio >= 1.2, adx < 25, bb_width > 1%

    숏 (완전 대칭):
      prev_close > bb_upper, close < bb_upper (반등 확인),
      rsi < prev_rsi (RSI 하향), rsi >= 60,
      volume_ratio >= 1.2, adx < 25, bb_width > 1%
    """
    # 이전봉 필요
    if bar_idx < 1:
        return None

    row = df.iloc[bar_idx]
    prev = df.iloc[bar_idx - 1]

    close = row.get("close", 0)
    rsi = row.get("rsi", 50)
    bb_lower = row.get("bb_lower", 0)
    bb_upper = row.get("bb_upper", 0)
    volume_ratio = row.get("volume_ratio", 0)
    adx = row.get("adx", 50)
    bb_width = row.get("bb_width", 0)
    atr = row.get("atr", 0)

    prev_close = prev.get("close", 0)
    prev_rsi = prev.get("rsi", 50)

    # 공통 필터
    if adx >= 30 or volume_ratio < 0.8 or bb_width <= 0.005 or atr <= 0:
        return None

    # ── 롱 (반등확인형) ──
    if (prev_close < bb_lower        # 이전봉이 BB하단 아래
            and close > bb_lower      # 현재봉이 밴드 안으로 복귀
            and rsi > prev_rsi        # RSI 상향 반전
            and rsi <= 45):           # 아직 과매도 근처
        entry = close
        sl, tp = _calc_sl_tp(entry, "long", atr, asset)
        strength = _box_strength(rsi, volume_ratio, bb_width, "long")
        return Signal(
            symbol=symbol, side="long", strategy="box",
            strength=strength, entry_price=entry,
            sl_price=sl, tp_price=tp,
            reason=f"BOX반등확인: prev<BB하단→복귀, RSI↑{rsi:.0f}, Vol={volume_ratio:.1f}x",
            regime=MarketRegime.BOX,
        )

    # ── 숏 (반등확인형, 완전 대칭) ──
    if (prev_close > bb_upper        # 이전봉이 BB상단 위
            and close < bb_upper      # 현재봉이 밴드 안으로 복귀
            and rsi < prev_rsi        # RSI 하향 반전
            and rsi >= 55):           # 아직 과매수 근처
        entry = close
        sl, tp = _calc_sl_tp(entry, "short", atr, asset)
        strength = _box_strength(rsi, volume_ratio, bb_width, "short")
        return Signal(
            symbol=symbol, side="short", strategy="box",
            strength=strength, entry_price=entry,
            sl_price=sl, tp_price=tp,
            reason=f"BOX반등확인: prev>BB상단→복귀, RSI↓{rsi:.0f}, Vol={volume_ratio:.1f}x",
            regime=MarketRegime.BOX,
        )

    return None


def _box_strength(
    rsi: float, volume_ratio: float, bb_width: float, side: str
) -> float:
    """BOX 반등확인형 신호 강도 0~1 계산."""
    if side == "long":
        rsi_s = min((40 - rsi) / 20, 1.0) if rsi <= 40 else 0
    else:
        rsi_s = min((rsi - 60) / 20, 1.0) if rsi >= 60 else 0
    vr_s = min((volume_ratio - 1.2) / 1.8, 1.0) if volume_ratio >= 1.2 else 0
    bb_s = min(bb_width / 0.05, 1.0)
    return round(max(0.0, min(1.0, rsi_s * 0.4 + vr_s * 0.3 + bb_s * 0.3)), 3)


# ──────────────────────────────────────────────
# SL / TP 계산 (ATR R 기반)
# ──────────────────────────────────────────────

def _calc_sl_tp(
    entry: float, side: str, atr: float, asset: str
) -> tuple[float, float]:
    """ATR R 기반 SL/TP 가격 계산.

    Args:
        entry: 진입 가격 (USDT)
        side: "long" / "short"
        atr: 현재 ATR 값 (USDT)
        asset: "BTC" / "ETH"

    Returns:
        (sl_price, tp_price)
    """
    sl_mult = CONFIG.SL_ATR_MULT.get(asset, 1.5)
    r_distance = atr * sl_mult  # 1R 가격 거리 (USDT)

    if side == "long":
        sl = entry - r_distance
        tp = entry + r_distance * CONFIG.TP2_R  # 사이징 RR은 TP2 기준 (부분청산 전략)
    else:
        sl = entry + r_distance
        tp = entry - r_distance * CONFIG.TP2_R

    return round(sl, 8), round(tp, 8)


def _get_asset(symbol: str) -> str:
    """심볼에서 자산명 추출."""
    if "BTC" in symbol.upper():
        return "BTC"
    return "ETH"
