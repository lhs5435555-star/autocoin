"""
v11 1m 체결 트리거.

15m 셋업 확정 후 1m에서 진입 타이밍 포착.
SL은 반드시 15m 구조 기준 (1m SL 사용 금지 — fee_R 폭증).
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Optional, Tuple

import pandas as pd

from fang_v10.setup_detector_15m import Setup

logger = logging.getLogger(__name__)


@dataclass
class EntrySignal:
    """1m 트리거 발화 결과."""
    symbol: str
    side: str                   # "long" / "short"
    entry_price: float          # 트리거 시점 1m 종가
    sl_price: float             # setup.sl_price (15m 기준, 절대 변경 금지)
    tp1: float
    tp2: float
    tp3: float
    profit_lock_price: float    # = tp1
    profit_lock_roe_pct: float
    atr_15m: float
    setup_id: str               # 추적용
    triggered_at: int           # ms timestamp


class EntryTrigger1m:
    """1m 봉 마감마다 호출. 트리거 조건 충족 시 EntrySignal 반환."""

    # Mark/Last 괴리 허용 비율 (SL 거리 대비)
    MARK_LAST_GAP_RATIO = 0.12

    # SL 최소 거리 (진입가 대비)
    MIN_SL_BUFFER_PCT = 0.003   # 0.3%

    # Funding veto 분
    FUNDING_VETO_MIN = 10.0

    # 1m 거래량 배수
    VOLUME_MULT = 1.2

    def on_1m_candle_closed(
        self,
        symbol: str,
        df_1m: pd.DataFrame,
        active_setup: Optional[Setup],
        mark_price: float,
        last_price: float,
        minutes_to_funding: float,
    ) -> Optional[EntrySignal]:
        """1m 봉 마감마다 호출."""

        if df_1m is None or df_1m.empty or len(df_1m) < 2:
            return None

        entry_est = float(df_1m.iloc[-1]["close"])

        # 1. 전제조건
        ok, reason = self._check_preconditions(
            active_setup, mark_price, last_price,
            minutes_to_funding, entry_est,
        )
        if not ok:
            # Funding/Mark-Last만 INFO, 나머지 DEBUG
            if "FUNDING" in reason or "MARK_LAST" in reason:
                logger.info("[1m ⛔] %s %s", symbol, reason)
            else:
                logger.debug("[1m ⛔] %s %s", symbol, reason)
            return None

        # 2. 1m 트리거
        assert active_setup is not None
        ok, trigger_reason = self._check_1m_trigger(
            df_1m, active_setup.side,
        )
        if not ok:
            return None

        # 3. 발화
        now_ms = int(time.time() * 1000)
        signal = EntrySignal(
            symbol=symbol,
            side=active_setup.side,
            entry_price=round(entry_est, 2),
            sl_price=active_setup.sl_price,
            tp1=active_setup.tp1,
            tp2=active_setup.tp2,
            tp3=active_setup.tp3,
            profit_lock_price=active_setup.tp1,   # == tp1
            profit_lock_roe_pct=0.0,
            atr_15m=active_setup.atr_15m,
            setup_id=active_setup.setup_id,
            triggered_at=now_ms,
        )

        logger.info(
            "[1m 🔥] %s %s @ %.0f | sl=%.0f | tp1=%.0f | setup_id=%s",
            symbol, active_setup.side.upper(), entry_est,
            active_setup.sl_price, active_setup.tp1,
            active_setup.setup_id,
        )

        return signal

    # ──────────────────────────────────────
    # 전제조건 검사
    # ──────────────────────────────────────

    def _check_preconditions(
        self,
        active_setup: Optional[Setup],
        mark_price: float,
        last_price: float,
        minutes_to_funding: float,
        entry_est: float,
    ) -> Tuple[bool, str]:
        """진입 전제조건 6개 검사."""

        # 1. 셋업 존재
        if active_setup is None:
            return False, "NO_SETUP"

        # 2. 미사용
        if active_setup.used:
            return False, "SETUP_ALREADY_USED"

        # 3. 만료 체크
        now_ms = int(time.time() * 1000)
        if now_ms > active_setup.expires_at_ts:
            logger.info(
                "[SETUP ⏰] %s %s 셋업 만료 | 미사용 | setup_id=%s",
                active_setup.symbol, active_setup.side,
                active_setup.setup_id,
            )
            return False, "SETUP_EXPIRED"

        # 4. Funding veto
        if minutes_to_funding <= self.FUNDING_VETO_MIN:
            return False, f"FUNDING_VETO: {minutes_to_funding:.1f}분 남음"

        # 5. Mark/Last 괴리
        if entry_est > 0:
            sl_dist = abs(entry_est - active_setup.sl_price) / entry_est
            mark_last_gap = abs(mark_price - last_price) / entry_est
            max_gap = sl_dist * self.MARK_LAST_GAP_RATIO
            if max_gap > 0 and mark_last_gap > max_gap:
                return False, (
                    f"MARK_LAST_GAP: gap={mark_last_gap:.4%} > "
                    f"limit={max_gap:.4%}"
                )

        # 6. SL 너무 가까움
        if active_setup.side == "long":
            if last_price < active_setup.sl_price * (1 + self.MIN_SL_BUFFER_PCT):
                return False, "TOO_CLOSE_TO_SL"
        else:
            if last_price > active_setup.sl_price * (1 - self.MIN_SL_BUFFER_PCT):
                return False, "TOO_CLOSE_TO_SL"

        return True, "OK"

    # ──────────────────────────────────────
    # 1m 트리거 조건
    # ──────────────────────────────────────

    def _check_1m_trigger(
        self, df_1m: pd.DataFrame, side: str,
    ) -> Tuple[bool, str]:
        """1m 트리거 조건 3개."""

        if len(df_1m) < 21:
            return False, "INSUFFICIENT_DATA"

        curr = df_1m.iloc[-1]
        prev = df_1m.iloc[-2]

        curr_open = float(curr["open"])
        curr_close = float(curr["close"])
        curr_volume = float(curr["volume"])
        prev_high = float(prev["high"])
        prev_low = float(prev["low"])

        vol_ma20 = float(df_1m["volume"].iloc[-21:-1].mean())

        if side == "long":
            # 조건 1: 양봉
            if curr_close <= curr_open:
                return False, "NOT_BULLISH_CANDLE"
            # 조건 2: 종가 > 직전 고가 (미세 돌파)
            if curr_close <= prev_high:
                return False, "NO_MICRO_BREAKOUT"
            # 조건 3: 거래량 > MA20 × 1.2
            if vol_ma20 > 0 and curr_volume < vol_ma20 * self.VOLUME_MULT:
                return False, "LOW_VOLUME"
        else:
            # 숏 대칭
            # 조건 1: 음봉
            if curr_close >= curr_open:
                return False, "NOT_BEARISH_CANDLE"
            # 조건 2: 종가 < 직전 저가
            if curr_close >= prev_low:
                return False, "NO_MICRO_BREAKDOWN"
            # 조건 3: 거래량
            if vol_ma20 > 0 and curr_volume < vol_ma20 * self.VOLUME_MULT:
                return False, "LOW_VOLUME"

        return True, "TRIGGERED"
