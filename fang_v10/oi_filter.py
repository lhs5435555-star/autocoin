"""
v11 OI(미결제약정) 필터 + Funding 시간 계산.

OI = 3차 보조 필터 (방향→힘→참여 확인).
OI 단독 진입 신호 금지.
5m EMA 크로스로 방향성 판단 (EMA6 > EMA24).

⚠️ Bitget OI 히스토리 미제공 → 라이브 전용 필터.
   백테스트에서는 OI 필터 자동 비활성 (항상 True).
"""

from __future__ import annotations

import logging
import time
from collections import defaultdict, deque
from typing import Any, Dict, Optional

import pandas as pd

from fang_v10.config import CONFIG

logger = logging.getLogger(__name__)


class OIFilter:
    """5m OI EMA 크로스 필터.

    - 라이브: 봇 시작 후 실시간 OI 수집, 30개 이상 시 활성화
    - 백테스트: 항상 True (OI 히스토리 미제공)
    """

    BUFFER_SIZE = 50
    READY_THRESHOLD = 30

    def __init__(self, backtest_mode: bool = False):
        self.backtest_mode = backtest_mode
        self._oi_buffer: Dict[str, deque] = defaultdict(
            lambda: deque(maxlen=self.BUFFER_SIZE)
        )
        self._ready: Dict[str, bool] = {}

    def update(self, symbol: str, oi_value: float, timestamp: int = 0) -> None:
        """5m 봉 마감마다 호출. OI 값을 버퍼에 추가."""
        if timestamp == 0:
            timestamp = int(time.time() * 1000)
        self._oi_buffer[symbol].append({"ts": timestamp, "oi": oi_value})
        if len(self._oi_buffer[symbol]) >= self.READY_THRESHOLD:
            if not self._ready.get(symbol):
                self._ready[symbol] = True
                logger.info("[OI] %s 필터 활성화 (%d건 수집)",
                            symbol, len(self._oi_buffer[symbol]))

    def is_ready(self, symbol: str) -> bool:
        """OI 데이터 충분히 수집되었는지."""
        return self._ready.get(symbol, False)

    def is_bullish(self, symbol: str) -> bool:
        """롱 진입 OI 확인.

        EMA6 > EMA24 AND 최근 3봉 OI 증가.
        준비 안 됐거나 백테스트면 True (필터 비활성 = 통과).
        """
        if self.backtest_mode or not self._ready.get(symbol):
            return True

        buf = self._oi_buffer.get(symbol)
        if not buf or len(buf) < self.READY_THRESHOLD:
            return True

        oi_series = pd.Series([x["oi"] for x in buf])
        ema6 = oi_series.ewm(span=6, adjust=False).mean().iloc[-1]
        ema24 = oi_series.ewm(span=24, adjust=False).mean().iloc[-1]
        recent_delta = oi_series.diff().iloc[-3:].sum()

        return bool(ema6 > ema24 and recent_delta > 0)

    def is_bearish(self, symbol: str) -> bool:
        """숏 진입 OI 확인.

        EMA6 < EMA24 AND 최근 3봉 OI 감소.
        준비 안 됐거나 백테스트면 True (필터 비활성 = 통과).
        """
        if self.backtest_mode or not self._ready.get(symbol):
            return True

        buf = self._oi_buffer.get(symbol)
        if not buf or len(buf) < self.READY_THRESHOLD:
            return True

        oi_series = pd.Series([x["oi"] for x in buf])
        ema6 = oi_series.ewm(span=6, adjust=False).mean().iloc[-1]
        ema24 = oi_series.ewm(span=24, adjust=False).mean().iloc[-1]
        recent_delta = oi_series.diff().iloc[-3:].sum()

        return bool(ema6 < ema24 and recent_delta < 0)

    def get_status(self, symbol: str) -> Dict:
        """디버그용 상태 반환."""
        buf = self._oi_buffer.get(symbol)
        if not buf:
            return {"ready": False, "count": 0}

        oi_series = pd.Series([x["oi"] for x in buf])
        ema6 = oi_series.ewm(span=6, adjust=False).mean().iloc[-1]
        ema24 = oi_series.ewm(span=24, adjust=False).mean().iloc[-1]
        return {
            "ready": self._ready.get(symbol, False),
            "count": len(buf),
            "ema6": round(ema6, 2),
            "ema24": round(ema24, 2),
            "bullish": ema6 > ema24,
        }


# ══════════════════════════════════════════
# OI 수집 헬퍼 (거래소 API)
# ══════════════════════════════════════════

def fetch_current_oi(symbol: str, exchange: Any) -> Optional[float]:
    """현재 OI 조회. 실패 시 None."""
    try:
        result = exchange.fetch_open_interest(symbol)
        if result and "openInterestAmount" in result:
            return float(result["openInterestAmount"])
        if result and "openInterest" in result:
            return float(result["openInterest"])
    except Exception as e:
        logger.debug("[OI] %s 조회 실패: %s", symbol, e)
    return None


# ══════════════════════════════════════════
# Funding 시간 계산
# ══════════════════════════════════════════

def get_minutes_to_funding(symbol: str = "", exchange: Any = None) -> float:
    """다음 펀딩까지 남은 분.

    Bitget 펀딩 주기: 8시간 (28800초).
    실패 시 999.0 반환 (Funding Veto 미발동).
    """
    try:
        now_ms = int(time.time() * 1000)
        funding_interval_ms = 8 * 3600 * 1000  # 8시간
        next_funding_ms = ((now_ms // funding_interval_ms) + 1) * funding_interval_ms
        remaining_minutes = (next_funding_ms - now_ms) / 60000
        return remaining_minutes
    except Exception:
        return 999.0
