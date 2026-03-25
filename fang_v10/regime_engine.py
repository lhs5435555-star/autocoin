"""
3-Mode 시장 레짐 엔진.

MarketRegime: TREND / BOX / PROTECT (NEUTRAL, UNKNOWN 없음)
우선순위: PROTECT > TREND > BOX
"""

from __future__ import annotations

import logging
from enum import Enum
from typing import Dict, Optional

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


class MarketRegime(Enum):
    """시장 레짐 (항상 3개 중 하나)."""
    TREND = "TREND"
    BOX = "BOX"
    PROTECT = "PROTECT"


def ensure_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """필수 지표 계산. 이미 있는 컬럼은 재계산하지 않음.

    Args:
        df: OHLCV DataFrame (open, high, low, close, volume 필수)

    Returns:
        지표가 추가된 DataFrame (inplace)
    """
    close = df["close"]
    high = df["high"]
    low = df["low"]

    # ── EMA(9, 21, 50) ──
    for period in (9, 21, 50):
        col = f"ema{period}"
        if col not in df.columns:
            df[col] = close.ewm(span=period, adjust=False).mean()

    # ── ATR(14) ──
    if "atr" not in df.columns:
        tr = pd.concat([
            high - low,
            (high - close.shift(1)).abs(),
            (low - close.shift(1)).abs(),
        ], axis=1).max(axis=1)
        df["atr"] = tr.ewm(span=14, adjust=False).mean()

    # ── ATR % ──
    if "atr_pct" not in df.columns:
        df["atr_pct"] = df["atr"] / close

    # ── ADX(14) ──
    if "adx" not in df.columns:
        _calc_adx(df, period=14)

    # ── RSI(14) ──
    if "rsi" not in df.columns:
        delta = close.diff()
        gain = delta.clip(lower=0).ewm(alpha=1 / 14, adjust=False).mean()
        loss = (-delta.clip(upper=0)).ewm(alpha=1 / 14, adjust=False).mean()
        rs = gain / loss.replace(0, np.nan)
        df["rsi"] = 100 - (100 / (1 + rs))

    # ── Bollinger Band(20, 2) ──
    if "bb_upper" not in df.columns:
        sma20 = close.rolling(20).mean()
        std20 = close.rolling(20).std()
        df["bb_upper"] = sma20 + 2 * std20
        df["bb_lower"] = sma20 - 2 * std20
        df["bb_mid"] = sma20

    # ── BB width ──
    if "bb_width" not in df.columns:
        df["bb_width"] = (df["bb_upper"] - df["bb_lower"]) / df["bb_mid"]

    # ── Volume ratio ──
    if "volume_ratio" not in df.columns:
        vol_ma = df["volume"].rolling(20).mean()
        df["volume_ratio"] = df["volume"] / vol_ma.replace(0, np.nan)

    # ── Volatility z-score (ATR 기반, 50봉 기준) ──
    if "volatility_zscore" not in df.columns:
        atr_mean = df["atr"].rolling(50).mean()
        atr_std = df["atr"].rolling(50).std()
        df["volatility_zscore"] = (df["atr"] - atr_mean) / atr_std.replace(0, np.nan)

    return df


def _calc_adx(df: pd.DataFrame, period: int = 14) -> None:
    """ADX(14) 계산 후 df['adx']에 저장."""
    high = df["high"]
    low = df["low"]

    plus_dm = high.diff().clip(lower=0)
    minus_dm = (-low.diff()).clip(lower=0)

    # +DM이 -DM보다 작으면 0, 반대도 마찬가지
    plus_dm[plus_dm <= minus_dm] = 0
    minus_dm[minus_dm <= plus_dm] = 0

    atr = df["atr"]
    plus_di = 100 * (plus_dm.ewm(span=period, adjust=False).mean() / atr.replace(0, np.nan))
    minus_di = 100 * (minus_dm.ewm(span=period, adjust=False).mean() / atr.replace(0, np.nan))

    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    df["adx"] = dx.ewm(span=period, adjust=False).mean()


class RegimeEngine:
    """시장 레짐 판별기.

    우선순위:
      1. PROTECT: ATR zscore > 2.0 OR 24H range >= 5% OR 1봉 >= 2.5%
         OR (ADX 25~30 AND EMA 3/3 미달)
         해제 시 최소 6봉(30분) 유지
      2. TREND: ADX(14) >= 25 AND EMA 3/3 완전 정렬
      3. BOX: 나머지 전부
    """

    # PROTECT 최소 유지 봉 수
    PROTECT_MIN_BARS: int = 6

    def __init__(self) -> None:
        # 심볼별 PROTECT 진입 봉 인덱스 추적
        self._protect_since: Dict[str, Optional[int]] = {}

    def detect(self, symbol: str, df: pd.DataFrame, bar_idx: int) -> MarketRegime:
        """현재 봉 기준 시장 레짐 판별.

        Args:
            symbol: 심볼 (예: "BTC/USDT:USDT")
            df: ensure_indicators() 적용된 OHLCV DataFrame
            bar_idx: 현재 봉 인덱스 (df.index 기준 정수 위치)

        Returns:
            MarketRegime (TREND / BOX / PROTECT)
        """
        df = ensure_indicators(df)

        if bar_idx < 0 or bar_idx >= len(df):
            logger.warning("bar_idx=%d 범위 밖 → BOX 반환", bar_idx)
            return MarketRegime.BOX

        row = df.iloc[bar_idx]

        # ── 1) PROTECT 판정 ──
        is_protect_trigger = self._check_protect_trigger(df, bar_idx, row)
        protect_since = self._protect_since.get(symbol)

        if is_protect_trigger:
            if protect_since is None:
                self._protect_since[symbol] = bar_idx
            return MarketRegime.PROTECT

        # PROTECT 해제: 최소 6봉 유지
        if protect_since is not None:
            bars_in_protect = bar_idx - protect_since
            if bars_in_protect < self.PROTECT_MIN_BARS:
                return MarketRegime.PROTECT
            # 최소 유지 충족 + 트리거 해제 → PROTECT 해제
            self._protect_since[symbol] = None

        # ── 2) TREND 판정: ADX >= 25 AND EMA 3/3 완전 정렬 ──
        adx_val = row.get("adx", 0)
        ema9 = row.get("ema9", 0)
        ema21 = row.get("ema21", 0)
        ema50 = row.get("ema50", 0)

        # EMA9-EMA21 정렬이면 TREND (EMA50 엄격 요구 제거)
        ema_aligned = (ema9 > ema21) or (ema9 < ema21)

        if adx_val >= 25 and ema_aligned:
            # 교착 해소: ADX 25~30 + EMA 2/3만 정렬 → BOX
            # 여기서는 3/3 완전 정렬이므로 TREND
            return MarketRegime.TREND

        # ── 3) BOX: 나머지 전부 ──
        return MarketRegime.BOX

    def get_trend_direction(self, symbol: str, df: pd.DataFrame) -> str:
        """현재 추세 방향 판별.

        Args:
            symbol: 심볼
            df: ensure_indicators() 적용된 DataFrame

        Returns:
            "up" / "down" / "flat"
        """
        df = ensure_indicators(df)

        if len(df) < 50:
            return "flat"

        row = df.iloc[-1]
        ema9 = row.get("ema9", 0)
        ema21 = row.get("ema21", 0)
        ema50 = row.get("ema50", 0)

        if ema9 > ema21 > ema50:
            return "up"
        elif ema9 < ema21 < ema50:
            return "down"
        return "flat"

    @staticmethod
    def _check_protect_trigger(
        df: pd.DataFrame, bar_idx: int, row: pd.Series
    ) -> bool:
        """PROTECT 트리거 조건 확인.

        조건 (OR):
          - ATR z-score > 2.0
          - 24H(288봉) range >= 5%
          - 현재 1봉 변동 >= 2.5%
          - ADX 25~30 AND EMA 3/3 미달 (추세 전환 초기)
        """
        # ATR z-score (극단적 변동성만 — 3.0 이상)
        vz = row.get("volatility_zscore", 0)
        if pd.notna(vz) and vz > 3.0:
            return True

        # 1봉 변동률 (5% 이상 — 플래시 크래시급만)
        bar_open = row.get("open", 0)
        bar_close = row.get("close", 0)
        if bar_open > 0:
            bar_change = abs(bar_close - bar_open) / bar_open
            if bar_change >= 0.05:
                return True

        # 24H range 제거 — BTC는 거의 항상 5% 이상이므로 무의미
        # ADX 전환 구간 제거 — 너무 빈번하게 트리거됨

        return False
