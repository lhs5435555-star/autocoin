"""
v11 15m 눌림목 Setup 감지 엔진.

1H 레짐 방향으로만 셋업 생성. Pullback Depth 0.33~0.75 필터.
수익보호 발동 = TP1 (동일 가격).
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

from fang_v10.config import CONFIG
from fang_v10.regime_engine import RegimeResult, ensure_indicators

logger = logging.getLogger(__name__)


# ══════════════════════════════════════════
# Setup 데이터 클래스
# ══════════════════════════════════════════

@dataclass
class Setup:
    """15m 눌림목 셋업."""
    symbol: str
    side: str                   # "long" / "short"
    regime: str                 # "TREND_UP" / "TREND_DOWN"
    entry_est: float            # 예상 진입가 (15m 마지막 봉 종가)
    sl_price: float             # 15m 구조 기반 SL
    r_dollar: float             # 1R 금액 (USDT)
    tp1: float                  # entry + R × 1.5
    tp2: float                  # entry + R × 2.5
    tp3: float                  # entry + R × 4.0
    atr_15m: float
    pullback_depth: float       # 0.33~0.75
    created_at_ts: int          # 15m 봉 마감 timestamp (ms)
    expires_at_ts: int          # created_at_ts + 15min
    setup_id: str = ""          # uuid
    used: bool = False

    def __post_init__(self):
        if not self.setup_id:
            self.setup_id = str(uuid.uuid4())[:8]

    @property
    def is_expired(self) -> bool:
        import time
        return int(time.time() * 1000) > self.expires_at_ts

    @property
    def r_distance(self) -> float:
        return abs(self.entry_est - self.sl_price)


# ══════════════════════════════════════════
# 15m 셋업 감지기
# ══════════════════════════════════════════

class SetupDetector15m:
    """15m 눌림목 셋업 감지."""

    # Pullback depth 범위
    DEPTH_MIN = 0.33
    DEPTH_MAX = 0.75

    # SL 거리 범위
    SL_DIST_MIN_PCT = 0.0015   # 0.15%
    SL_DIST_MAX_PCT = 0.020    # 2.0%

    # TP R 배수
    TP1_R = 1.5

    def __init__(self, oi_filter=None):
        """oi_filter: OIFilter 인스턴스 (None이면 OI 조건 자동 통과)."""
        self.oi_filter = oi_filter
    TP2_R = 2.5
    TP3_R = 4.0

    # EMA 접촉 허용 범위
    EMA_TOUCH_MARGIN = 0.002   # 0.2%

    # EMA gap 최소
    EMA_GAP_MIN = 0.002        # 0.2%

    # ADX 최소
    ADX_MIN = 20

    def on_15m_candle_closed(
        self,
        symbol: str,
        df_15m: pd.DataFrame,
        regime: RegimeResult,
        active_setup: Optional[Setup] = None,
    ) -> Optional[Setup]:
        """15m 봉 마감마다 호출. Setup 또는 None 반환."""

        # 레짐 필터
        if regime.regime == "TREND_UP":
            side = "long"
        elif regime.regime == "TREND_DOWN":
            side = "short"
        else:
            return None

        # 기존 미사용 셋업 있으면 새 셋업 안 만듦
        if active_setup is not None and not active_setup.used:
            return None

        # 지표 확인
        df_15m = ensure_indicators(df_15m)
        if len(df_15m) < 50:
            return None

        # 셋업 조건 확인
        if side == "long":
            ok, diag = self._long_setup_ok(df_15m)
        else:
            ok, diag = self._short_setup_ok(df_15m)

        if not ok:
            failed = diag.get("failed_condition", "?")
            failed_name = diag.get("failed_name", "?")
            failed_val = diag.get("failed_value", "?")
            logger.info(
                "[SETUP ❌] %s 조건%s 실패 | %s: %s",
                symbol, failed, failed_name, failed_val,
            )
            return None

        # SL 계산
        try:
            if side == "long":
                sl = self._calc_sl_long(df_15m)
            else:
                sl = self._calc_sl_short(df_15m)
        except ValueError as e:
            logger.info("[SETUP ❌] %s SL 계산 실패: %s", symbol, e)
            return None

        entry_est = float(df_15m.iloc[-1]["close"])
        atr = float(df_15m.iloc[-1].get("atr", 0))

        # TP 계산
        targets = self._calc_targets(entry_est, sl, side)

        # R 금액 (사이징용 참고)
        r_dist = abs(entry_est - sl)
        r_dollar = r_dist  # 코인 1개 기준 R (실제 포지션 사이징은 sizing_engine에서)

        # timestamp
        raw_ts = df_15m.iloc[-1].get("timestamp", 0)
        if hasattr(raw_ts, 'timestamp'):
            ts = int(raw_ts.timestamp() * 1000)  # pandas Timestamp → ms
        else:
            ts = int(raw_ts) if raw_ts else 0
        if ts == 0:
            import time
            ts = int(time.time() * 1000)

        setup = Setup(
            symbol=symbol,
            side=side,
            regime=regime.regime,
            entry_est=round(entry_est, 2),
            sl_price=round(sl, 2),
            r_dollar=round(r_dollar, 4),
            tp1=round(targets["tp1"], 2),
            tp2=round(targets["tp2"], 2),
            tp3=round(targets["tp3"], 2),
            atr_15m=round(atr, 4),
            pullback_depth=round(diag.get("depth", 0), 4),
            created_at_ts=ts,
            expires_at_ts=ts + 15 * 60 * 1000,
        )

        logger.info(
            "[SETUP ✅] %s %s | entry≈%.0f | sl=%.0f | R=%.0f | "
            "depth=%.2f | tp1=%.0f | tp2=%.0f | tp3=%.0f",
            symbol, side.upper(), entry_est, sl, r_dist,
            setup.pullback_depth, targets["tp1"], targets["tp2"], targets["tp3"],
        )

        return setup

    # ──────────────────────────────────────
    # 롱 셋업 조건 (7개 AND)
    # ──────────────────────────────────────

    def _long_setup_ok(self, df: pd.DataFrame) -> Tuple[bool, Dict]:
        """롱 셋업 조건 7개 확인."""
        diag: Dict[str, Any] = {}
        row = df.iloc[-1]

        ema20 = float(df["close"].ewm(span=20, adjust=False).mean().iloc[-1])
        ema50 = float(df["close"].ewm(span=50, adjust=False).mean().iloc[-1])
        adx = float(row.get("adx", 0))
        plus_di = float(row.get("plus_di", 0)) if "plus_di" in row.index else adx * 0.6
        minus_di = float(row.get("minus_di", 0)) if "minus_di" in row.index else adx * 0.4
        close = float(row["close"])

        # 조건 1: ema20 > ema50
        if ema20 <= ema50:
            return False, {"failed_condition": "1", "failed_name": "ema20<=ema50",
                           "failed_value": f"ema20={ema20:.1f} ema50={ema50:.1f}"}

        # 조건 2: ADX >= 20
        if adx < self.ADX_MIN:
            return False, {"failed_condition": "2", "failed_name": "adx",
                           "failed_value": f"{adx:.1f} < {self.ADX_MIN}"}

        # 조건 3: +DI > -DI
        if plus_di <= minus_di:
            return False, {"failed_condition": "3", "failed_name": "+DI<=-DI",
                           "failed_value": f"+DI={plus_di:.1f} -DI={minus_di:.1f}"}

        # 조건 4: EMA gap >= 0.2%
        ema_gap = abs(ema20 - ema50) / ema50 if ema50 > 0 else 0
        if ema_gap < self.EMA_GAP_MIN:
            return False, {"failed_condition": "4", "failed_name": "ema_gap",
                           "failed_value": f"{ema_gap:.4f} < {self.EMA_GAP_MIN}"}

        # 조건 5: Pullback depth 0.33~0.75
        depth = self._calc_pullback_depth_long(df)
        diag["depth"] = depth
        if depth < self.DEPTH_MIN:
            return False, {"failed_condition": "5", "failed_name": "pullback_depth",
                           "failed_value": f"{depth:.3f} < {self.DEPTH_MIN} (추격 진입)",
                           "depth": depth}
        if depth > self.DEPTH_MAX:
            return False, {"failed_condition": "5", "failed_name": "pullback_depth",
                           "failed_value": f"{depth:.3f} > {self.DEPTH_MAX} (추세 붕괴)",
                           "depth": depth}

        # 조건 6: 최근 5봉 중 EMA 구간 접촉
        ema_low = ema50 * (1 - self.EMA_TOUCH_MARGIN)
        ema_high = ema20 * (1 + self.EMA_TOUCH_MARGIN)
        lows = df["low"].iloc[-6:-1].values if len(df) >= 6 else df["low"].values
        touched = any(ema_low <= l <= ema_high for l in lows)
        if not touched:
            return False, {"failed_condition": "6", "failed_name": "ema_touch",
                           "failed_value": f"최근5봉 저가 EMA구간 미접촉",
                           "depth": depth}

        # 조건 7: 현재 봉 종가 > ema20 (회복 확인)
        if close <= ema20:
            return False, {"failed_condition": "7", "failed_name": "close<=ema20",
                           "failed_value": f"close={close:.1f} ema20={ema20:.1f}",
                           "depth": depth}

        # 조건 8: OI 필터 (라이브 전용, 백테스트 자동 통과)
        if self.oi_filter is not None and not self.oi_filter.is_bullish(
                df.iloc[-1].get("symbol", "") if "symbol" in df.columns else ""):
            return False, {"failed_condition": "8", "failed_name": "oi_not_bullish",
                           "failed_value": "OI EMA6 <= EMA24",
                           "depth": depth}

        diag["ema20"] = ema20
        diag["ema50"] = ema50
        diag["adx"] = adx
        return True, diag

    # ──────────────────────────────────────
    # 숏 셋업 조건 (완전 대칭)
    # ──────────────────────────────────────

    def _short_setup_ok(self, df: pd.DataFrame) -> Tuple[bool, Dict]:
        """숏 셋업 조건 7개 확인 (롱 대칭)."""
        diag: Dict[str, Any] = {}
        row = df.iloc[-1]

        ema20 = float(df["close"].ewm(span=20, adjust=False).mean().iloc[-1])
        ema50 = float(df["close"].ewm(span=50, adjust=False).mean().iloc[-1])
        adx = float(row.get("adx", 0))
        plus_di = float(row.get("plus_di", 0)) if "plus_di" in row.index else adx * 0.4
        minus_di = float(row.get("minus_di", 0)) if "minus_di" in row.index else adx * 0.6
        close = float(row["close"])

        # 조건 1: ema20 < ema50
        if ema20 >= ema50:
            return False, {"failed_condition": "1", "failed_name": "ema20>=ema50",
                           "failed_value": f"ema20={ema20:.1f} ema50={ema50:.1f}"}

        # 조건 2: ADX >= 20
        if adx < self.ADX_MIN:
            return False, {"failed_condition": "2", "failed_name": "adx",
                           "failed_value": f"{adx:.1f} < {self.ADX_MIN}"}

        # 조건 3: -DI > +DI
        if minus_di <= plus_di:
            return False, {"failed_condition": "3", "failed_name": "-DI<=+DI",
                           "failed_value": f"+DI={plus_di:.1f} -DI={minus_di:.1f}"}

        # 조건 4: EMA gap >= 0.2%
        ema_gap = abs(ema20 - ema50) / ema50 if ema50 > 0 else 0
        if ema_gap < self.EMA_GAP_MIN:
            return False, {"failed_condition": "4", "failed_name": "ema_gap",
                           "failed_value": f"{ema_gap:.4f} < {self.EMA_GAP_MIN}"}

        # 조건 5: Pullback depth 0.33~0.75
        depth = self._calc_pullback_depth_short(df)
        diag["depth"] = depth
        if depth < self.DEPTH_MIN:
            return False, {"failed_condition": "5", "failed_name": "pullback_depth",
                           "failed_value": f"{depth:.3f} < {self.DEPTH_MIN} (추격 진입)",
                           "depth": depth}
        if depth > self.DEPTH_MAX:
            return False, {"failed_condition": "5", "failed_name": "pullback_depth",
                           "failed_value": f"{depth:.3f} > {self.DEPTH_MAX} (추세 붕괴)",
                           "depth": depth}

        # 조건 6: 최근 5봉 중 EMA 구간 접촉 (숏: 고가가 EMA 구간에 접촉)
        ema_low = ema20 * (1 - self.EMA_TOUCH_MARGIN)
        ema_high = ema50 * (1 + self.EMA_TOUCH_MARGIN)
        highs = df["high"].iloc[-6:-1].values if len(df) >= 6 else df["high"].values
        touched = any(ema_low <= h <= ema_high for h in highs)
        if not touched:
            return False, {"failed_condition": "6", "failed_name": "ema_touch",
                           "failed_value": "최근5봉 고가 EMA구간 미접촉",
                           "depth": depth}

        # 조건 7: 현재 봉 종가 < ema20 (회복 확인 = 하방 재진입)
        if close >= ema20:
            return False, {"failed_condition": "7", "failed_name": "close>=ema20",
                           "failed_value": f"close={close:.1f} ema20={ema20:.1f}",
                           "depth": depth}

        # 조건 8: OI 필터 (라이브 전용, 백테스트 자동 통과)
        if self.oi_filter is not None and not self.oi_filter.is_bearish(
                df.iloc[-1].get("symbol", "") if "symbol" in df.columns else ""):
            return False, {"failed_condition": "8", "failed_name": "oi_not_bearish",
                           "failed_value": "OI EMA6 >= EMA24",
                           "depth": depth}

        diag["ema20"] = ema20
        diag["ema50"] = ema50
        diag["adx"] = adx
        return True, diag

    # ──────────────────────────────────────
    # Pullback Depth 계산
    # ──────────────────────────────────────

    @staticmethod
    def _calc_pullback_depth_long(df: pd.DataFrame) -> float:
        """롱 눌림 깊이: 임펄스 고점 대비 얼마나 빠졌나."""
        if len(df) < 21:
            return 0.0
        impulse_high = float(df["high"].iloc[-21:-1].max())
        impulse_low = float(df["low"].iloc[-21:-1].min())
        pullback_low = float(df["low"].iloc[-6:-1].min()) if len(df) >= 6 else impulse_low
        denom = impulse_high - impulse_low + 1e-9
        return (impulse_high - pullback_low) / denom

    @staticmethod
    def _calc_pullback_depth_short(df: pd.DataFrame) -> float:
        """숏 눌림 깊이: 임펄스 저점 대비 얼마나 올랐나."""
        if len(df) < 21:
            return 0.0
        impulse_high = float(df["high"].iloc[-21:-1].max())
        impulse_low = float(df["low"].iloc[-21:-1].min())
        pullback_high = float(df["high"].iloc[-6:-1].max()) if len(df) >= 6 else impulse_high
        denom = impulse_high - impulse_low + 1e-9
        return (pullback_high - impulse_low) / denom

    # ──────────────────────────────────────
    # SL 계산
    # ──────────────────────────────────────

    def _calc_sl_long(self, df: pd.DataFrame) -> float:
        """롱 SL = min(최근 7봉 저가) - ATR14 × 0.3."""
        atr = float(df.iloc[-1].get("atr", 0))
        recent_low = float(df["low"].iloc[-8:-1].min()) if len(df) >= 8 else float(df["low"].min())
        sl = recent_low - atr * 0.3
        entry = float(df.iloc[-1]["close"])
        sl_dist = (entry - sl) / entry
        if sl_dist < self.SL_DIST_MIN_PCT:
            raise ValueError(f"SL 너무 타이트: {sl_dist:.4%} < {self.SL_DIST_MIN_PCT:.4%}")
        if sl_dist > self.SL_DIST_MAX_PCT:
            raise ValueError(f"SL 너무 넓음: {sl_dist:.4%} > {self.SL_DIST_MAX_PCT:.4%}")
        return sl

    def _calc_sl_short(self, df: pd.DataFrame) -> float:
        """숏 SL = max(최근 7봉 고가) + ATR14 × 0.3."""
        atr = float(df.iloc[-1].get("atr", 0))
        recent_high = float(df["high"].iloc[-8:-1].max()) if len(df) >= 8 else float(df["high"].max())
        sl = recent_high + atr * 0.3
        entry = float(df.iloc[-1]["close"])
        sl_dist = (sl - entry) / entry
        if sl_dist < self.SL_DIST_MIN_PCT:
            raise ValueError(f"SL 너무 타이트: {sl_dist:.4%} < {self.SL_DIST_MIN_PCT:.4%}")
        if sl_dist > self.SL_DIST_MAX_PCT:
            raise ValueError(f"SL 너무 넓음: {sl_dist:.4%} > {self.SL_DIST_MAX_PCT:.4%}")
        return sl

    # ──────────────────────────────────────
    # TP 계산
    # ──────────────────────────────────────

    def _calc_targets(self, entry: float, sl: float, side: str) -> Dict[str, float]:
        """TP1/TP2/TP3 + 수익보호 가격 계산.

        ★ profit_lock_price == tp1 (반드시 동일)
        """
        r = abs(entry - sl)
        sign = 1.0 if side == "long" else -1.0

        tp1 = entry + sign * r * self.TP1_R
        tp2 = entry + sign * r * self.TP2_R
        tp3 = entry + sign * r * self.TP3_R

        leverage = CONFIG.LEVERAGE_MAX.get(
            "BTC" if "BTC" in str(entry) else "ETH", 10
        )
        profit_lock_roe = (r * self.TP1_R / entry) * leverage * 100

        return {
            "tp1": tp1,
            "tp2": tp2,
            "tp3": tp3,
            "profit_lock_price": tp1,       # ★ = tp1
            "profit_lock_roe_pct": round(profit_lock_roe, 2),
        }
