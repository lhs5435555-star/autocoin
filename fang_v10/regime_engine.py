"""
v11 1H 완성봉 레짐 엔진.

4-Mode: TREND_UP / TREND_DOWN / BOX / NO_TRADE
1H 완성봉만 사용 — 5m/15m 노이즈로 레짐을 절대 변경하지 않음.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, asdict, field
from enum import Enum
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd

from fang_v10.config import CONFIG

logger = logging.getLogger(__name__)


# ══════════════════════════════════════════
# 레짐 타입 (v10 호환 + v11 확장)
# ══════════════════════════════════════════

class MarketRegime(Enum):
    """시장 레짐."""
    TREND = "TREND"           # v10 호환 (TREND_UP/DOWN 을 TREND로 매핑)
    BOX = "BOX"
    PROTECT = "PROTECT"       # v10 호환 (NO_TRADE 를 PROTECT로 매핑)
    TREND_UP = "TREND_UP"
    TREND_DOWN = "TREND_DOWN"
    NO_TRADE = "NO_TRADE"


@dataclass
class RegimeResult:
    """레짐 판단 결과."""
    symbol: str
    regime: str                 # "TREND_UP" / "TREND_DOWN" / "BOX" / "NO_TRADE"
    adx: float = 0.0
    plus_di: float = 0.0
    minus_di: float = 0.0
    ema20: float = 0.0
    ema50: float = 0.0
    ema_gap_pct: float = 0.0    # abs(ema20-ema50)/ema50 * 100
    ema20_slope: float = 0.0
    updated_at: int = 0         # 마지막 1H 봉 timestamp (ms)

    @property
    def is_trend(self) -> bool:
        return self.regime in ("TREND_UP", "TREND_DOWN")

    @property
    def v10_regime(self) -> MarketRegime:
        """v10 호환 MarketRegime 반환."""
        if self.regime == "TREND_UP":
            return MarketRegime.TREND
        elif self.regime == "TREND_DOWN":
            return MarketRegime.TREND
        elif self.regime == "NO_TRADE":
            return MarketRegime.PROTECT
        return MarketRegime.BOX


# ══════════════════════════════════════════
# 15m 지표 계산 (전략용 — v10 ensure_indicators 대체)
# ══════════════════════════════════════════

def ensure_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """15m 전략용 필수 지표 계산. 이미 있는 컬럼은 재계산하지 않음."""
    if df is None or df.empty:
        return df

    close = df["close"]
    high = df["high"]
    low = df["low"]

    for period in (9, 21, 50):
        col = f"ema{period}"
        if col not in df.columns:
            df[col] = close.ewm(span=period, adjust=False).mean()

    if "atr" not in df.columns:
        tr = pd.concat([
            high - low,
            (high - close.shift(1)).abs(),
            (low - close.shift(1)).abs(),
        ], axis=1).max(axis=1)
        df["atr"] = tr.ewm(span=14, adjust=False).mean()

    if "atr_pct" not in df.columns:
        df["atr_pct"] = df["atr"] / close

    if "adx" not in df.columns:
        _calc_adx_full(df, period=14)

    if "rsi" not in df.columns:
        delta = close.diff()
        gain = delta.clip(lower=0).ewm(alpha=1 / 14, adjust=False).mean()
        loss = (-delta.clip(upper=0)).ewm(alpha=1 / 14, adjust=False).mean()
        rs = gain / loss.replace(0, np.nan)
        df["rsi"] = 100 - (100 / (1 + rs))

    if "bb_upper" not in df.columns:
        sma20 = close.rolling(20).mean()
        std20 = close.rolling(20).std()
        df["bb_upper"] = sma20 + 2 * std20
        df["bb_lower"] = sma20 - 2 * std20
        df["bb_mid"] = sma20

    if "bb_width" not in df.columns:
        df["bb_width"] = (df["bb_upper"] - df["bb_lower"]) / df["bb_mid"]

    if "volume_ratio" not in df.columns:
        vol_ma = df["volume"].rolling(20).mean()
        df["volume_ratio"] = df["volume"] / vol_ma.replace(0, np.nan)

    if "volatility_zscore" not in df.columns:
        atr_mean = df["atr"].rolling(50).mean()
        atr_std = df["atr"].rolling(50).std()
        df["volatility_zscore"] = (df["atr"] - atr_mean) / atr_std.replace(0, np.nan)

    return df


def _calc_adx_full(df: pd.DataFrame, period: int = 14) -> None:
    """ADX + plus_di + minus_di 계산."""
    high = df["high"]
    low = df["low"]

    plus_dm = high.diff().clip(lower=0)
    minus_dm = (-low.diff()).clip(lower=0)

    plus_dm[plus_dm <= minus_dm] = 0
    minus_dm[minus_dm <= plus_dm] = 0

    atr = df["atr"]
    plus_di = 100 * (plus_dm.ewm(span=period, adjust=False).mean() / atr.replace(0, np.nan))
    minus_di = 100 * (minus_dm.ewm(span=period, adjust=False).mean() / atr.replace(0, np.nan))

    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    df["adx"] = dx.ewm(span=period, adjust=False).mean()
    df["plus_di"] = plus_di
    df["minus_di"] = minus_di


# ══════════════════════════════════════════
# 1H 지표 계산 (레짐 전용)
# ══════════════════════════════════════════

def compute_1h_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """1H 레짐 전용 지표 계산.

    추가 컬럼: ema20, ema50, ema20_slope, adx14, plus_di, minus_di
    """
    if df is None or df.empty or len(df) < 2:
        return df

    close = df["close"]
    high = df["high"]
    low = df["low"]

    df["ema20"] = close.ewm(span=20, adjust=False).mean()
    df["ema50"] = close.ewm(span=50, adjust=False).mean()
    df["ema20_slope"] = df["ema20"].diff()

    # ATR for ADX calculation
    tr = pd.concat([
        high - low,
        (high - close.shift(1)).abs(),
        (low - close.shift(1)).abs(),
    ], axis=1).max(axis=1)
    df["atr_1h"] = tr.ewm(span=14, adjust=False).mean()

    # ADX + DI
    plus_dm = high.diff().clip(lower=0)
    minus_dm = (-low.diff()).clip(lower=0)
    plus_dm[plus_dm <= minus_dm] = 0
    minus_dm[minus_dm <= plus_dm] = 0

    atr_safe = df["atr_1h"].replace(0, np.nan)
    df["plus_di"] = 100 * (plus_dm.ewm(span=14, adjust=False).mean() / atr_safe)
    df["minus_di"] = 100 * (minus_dm.ewm(span=14, adjust=False).mean() / atr_safe)

    di_sum = (df["plus_di"] + df["minus_di"]).replace(0, np.nan)
    dx = 100 * (df["plus_di"] - df["minus_di"]).abs() / di_sum
    df["adx14"] = dx.ewm(span=14, adjust=False).mean()

    return df


def resample_to_1h(df_15m: pd.DataFrame) -> pd.DataFrame:
    """15m DataFrame을 1H로 리샘플 (백테스트용).

    15m 4봉 = 1H 1봉. 미완성 1H 봉은 제거.
    """
    if df_15m is None or df_15m.empty:
        return pd.DataFrame()

    # timestamp 기반 리샘플
    df = df_15m.copy()
    if "timestamp" in df.columns:
        ts_vals = df["timestamp"]
        # 실제 ms timestamp인지 확인 (> 1e12)
        first_ts = ts_vals.iloc[0]
        if hasattr(first_ts, 'timestamp'):
            # 이미 pandas Timestamp
            df["dt"] = pd.to_datetime(ts_vals)
        elif float(first_ts) > 1e12:
            df["dt"] = pd.to_datetime(ts_vals, unit="ms")
        else:
            # 테스트 데이터 (순번) → 15분 간격으로 가상 timestamp 생성
            base = pd.Timestamp("2025-01-01")
            df["dt"] = [base + pd.Timedelta(minutes=15 * i) for i in range(len(df))]
    else:
        base = pd.Timestamp("2025-01-01")
        df["dt"] = [base + pd.Timedelta(minutes=15 * i) for i in range(len(df))]

    df = df.set_index("dt")
    ohlcv_1h = df.resample("1h").agg({
        "open": "first",
        "high": "max",
        "low": "min",
        "close": "last",
        "volume": "sum",
    }).dropna(subset=["open"])

    # 미완성 마지막 봉 제거 (4봉 미만)
    if len(df) > 0:
        last_1h_start = ohlcv_1h.index[-1] if len(ohlcv_1h) > 0 else None
        if last_1h_start is not None:
            bars_in_last = len(df[df.index >= last_1h_start])
            if bars_in_last < 4:
                ohlcv_1h = ohlcv_1h.iloc[:-1]

    # timestamp 복원
    ohlcv_1h["timestamp"] = ohlcv_1h.index.astype(np.int64) // 10**6
    ohlcv_1h = ohlcv_1h.reset_index(drop=True)

    return ohlcv_1h


# ══════════════════════════════════════════
# 레짐 판단 함수
# ══════════════════════════════════════════

ADX_THRESHOLD = 18  # TREND 판정 ADX 임계값


def detect_regime(symbol: str, df_1h: pd.DataFrame,
                  df_5m: pd.DataFrame = None) -> RegimeResult:
    """1H 완성봉 기반 레짐 판단.

    우선순위:
      1. NO_TRADE: 5m ATR 스파이크 (ATR > MA20 * 2.5)
      2. TREND_UP: ema20 > ema50, slope > 0, ADX >= 18, +DI > -DI
      3. TREND_DOWN: ema20 < ema50, slope < 0, ADX >= 18, -DI > +DI
      4. BOX: 나머지
    """
    default = RegimeResult(symbol=symbol, regime="BOX")

    if df_1h is None or df_1h.empty or len(df_1h) < 50:
        return default

    row = df_1h.iloc[-1]
    raw_ts = row.get("timestamp", 0)
    ts = int(raw_ts.timestamp() * 1000) if hasattr(raw_ts, 'timestamp') else int(raw_ts or 0)

    ema20 = float(row.get("ema20", 0))
    ema50 = float(row.get("ema50", 0))
    ema20_slope = float(row.get("ema20_slope", 0))
    adx = float(row.get("adx14", 0))
    plus_di = float(row.get("plus_di", 0))
    minus_di = float(row.get("minus_di", 0))

    ema_gap_pct = abs(ema20 - ema50) / ema50 * 100 if ema50 > 0 else 0

    base = RegimeResult(
        symbol=symbol, regime="BOX",
        adx=round(adx, 2), plus_di=round(plus_di, 2),
        minus_di=round(minus_di, 2),
        ema20=round(ema20, 2), ema50=round(ema50, 2),
        ema_gap_pct=round(ema_gap_pct, 3),
        ema20_slope=round(ema20_slope, 4),
        updated_at=ts,
    )

    # 1. NO_TRADE: 5m ATR 스파이크
    if df_5m is not None and len(df_5m) > 20:
        df_5m = ensure_indicators(df_5m)
        atr_5m = float(df_5m.iloc[-1].get("atr", 0))
        atr_5m_ma20 = float(df_5m["atr"].rolling(20).mean().iloc[-1])
        if atr_5m_ma20 > 0 and atr_5m > atr_5m_ma20 * 2.5:
            base.regime = "NO_TRADE"
            return base

    # 2. TREND_UP
    if (ema20 > ema50
            and ema20_slope > 0
            and adx >= ADX_THRESHOLD
            and plus_di > minus_di):
        base.regime = "TREND_UP"
        return base

    # 3. TREND_DOWN
    if (ema20 < ema50
            and ema20_slope < 0
            and adx >= ADX_THRESHOLD
            and minus_di > plus_di):
        base.regime = "TREND_DOWN"
        return base

    # 4. BOX
    base.regime = "BOX"
    return base


# ══════════════════════════════════════════
# 레짐 엔진 (캐시 + 1H 봉 마감 기준 업데이트)
# ══════════════════════════════════════════

class RegimeEngine:
    """1H 완성봉 레짐 엔진.

    1H 봉이 새로 마감됐을 때만 레짐을 업데이트한다.
    봉 마감 사이에는 캐시된 레짐을 그대로 반환한다.
    """

    def __init__(self) -> None:
        self._cache: Dict[str, RegimeResult] = {}
        self._last_1h_ts: Dict[str, int] = {}

    def get_regime(self, symbol: str, df_1h: pd.DataFrame,
                   df_5m: pd.DataFrame = None) -> RegimeResult:
        """레짐 조회 (1H 봉 마감 시에만 재계산).

        Args:
            symbol: 심볼
            df_1h: 1H OHLCV + indicators (compute_1h_indicators 적용)
            df_5m: 5m OHLCV (NO_TRADE 판단용, 없어도 됨)

        Returns:
            RegimeResult
        """
        if df_1h is None or df_1h.empty:
            return self._cache.get(symbol, RegimeResult(symbol=symbol, regime="BOX"))

        _raw = df_1h.iloc[-1].get("timestamp", 0)
        latest_ts = int(_raw.timestamp() * 1000) if hasattr(_raw, 'timestamp') else int(_raw or 0)

        if latest_ts != self._last_1h_ts.get(symbol):
            result = detect_regime(symbol, df_1h, df_5m)
            self._cache[symbol] = result
            self._last_1h_ts[symbol] = latest_ts
            logger.info(
                "[REGIME] %s → %s | ADX=%.1f +DI=%.1f -DI=%.1f "
                "EMA20=%.1f EMA50=%.1f gap=%.2f%% slope=%.2f",
                symbol, result.regime, result.adx,
                result.plus_di, result.minus_di,
                result.ema20, result.ema50,
                result.ema_gap_pct, result.ema20_slope,
            )

        return self._cache.get(symbol, RegimeResult(symbol=symbol, regime="BOX"))

    # ── v10 호환 인터페이스 ──

    def detect(self, symbol: str, df: pd.DataFrame, bar_idx: int = -1) -> MarketRegime:
        """v10 호환: df에서 레짐 판단.

        timestamp 컬럼이 있으면 1H 리샘플, 없으면 직접 지표 사용.
        """
        if df is None or df.empty:
            return MarketRegime.BOX

        subset = df.iloc[:bar_idx + 1] if 0 <= bar_idx < len(df) else df

        # timestamp가 있으면 1H 리샘플 (실데이터/백테스트)
        if "timestamp" in subset.columns and len(subset) >= 50:
            df_1h = resample_to_1h(subset)
            if not df_1h.empty and len(df_1h) >= 50:
                df_1h = compute_1h_indicators(df_1h)
                result = detect_regime(symbol, df_1h)
                return result.v10_regime

        # fallback: 15m 데이터에서 직접 판단 (테스트/짧은 데이터)
        subset = ensure_indicators(subset)
        if len(subset) < 2:
            return MarketRegime.BOX

        row = subset.iloc[-1]
        adx_val = float(row.get("adx", 0))
        ema9 = float(row.get("ema9", 0))
        ema21 = float(row.get("ema21", 0))

        # NO_TRADE (극단 변동성)
        vz = row.get("volatility_zscore", 0)
        if pd.notna(vz) and vz > 3.0:
            return MarketRegime.PROTECT

        bar_open = float(row.get("open", 0))
        bar_close = float(row.get("close", 0))
        if bar_open > 0 and abs(bar_close - bar_open) / bar_open >= 0.05:
            return MarketRegime.PROTECT

        if adx_val >= ADX_THRESHOLD and ema9 != ema21:
            return MarketRegime.TREND

        return MarketRegime.BOX

    def get_trend_direction(self, symbol: str, df: pd.DataFrame = None) -> str:
        """현재 추세 방향."""
        cached = self._cache.get(symbol)
        if cached:
            if cached.regime == "TREND_UP":
                return "up"
            elif cached.regime == "TREND_DOWN":
                return "down"
        return "flat"

    # ── 상태 저장/복원 ──

    def save_to_state(self, state_store: Any) -> None:
        """레짐 캐시 저장."""
        data = {s: asdict(r) for s, r in self._cache.items()}
        try:
            state_store.save_regime(data)
        except AttributeError:
            pass  # state_store에 save_regime 없으면 무시

    def load_from_state(self, state_store: Any) -> None:
        """레짐 캐시 복원."""
        try:
            cached = state_store.load_regime()
            for symbol, d in cached.items():
                self._cache[symbol] = RegimeResult(**d)
                self._last_1h_ts[symbol] = d.get("updated_at", 0)
        except (AttributeError, TypeError):
            pass
