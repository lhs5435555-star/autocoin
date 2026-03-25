"""
BTC 방향 필터 — ETH 진입 전 BTC EMA 방향 확인.

BTC EMA9 > EMA21 → ETH SHORT 차단
BTC EMA9 < EMA21 → ETH LONG 차단
BTC flat (차이 < 0.1%) → 양방향 허용
데이터 부족 시 → 통과 (True, "데이터부족")
"""

from __future__ import annotations

import logging
from typing import Tuple

import pandas as pd

from fang_v10.regime_engine import ensure_indicators

logger = logging.getLogger(__name__)

# BTC EMA9-EMA21 차이가 이 비율 미만이면 flat 판정
FLAT_THRESHOLD: float = 0.001  # 0.1%


def can_enter_eth(btc_df: pd.DataFrame, eth_side: str) -> Tuple[bool, str]:
    """BTC 방향 기반 ETH 진입 가능 여부 판단.

    Args:
        btc_df: BTC OHLCV DataFrame (ensure_indicators 미적용 가능)
        eth_side: "long" 또는 "short" (대소문자 무관)

    Returns:
        (allowed: bool, reason: str)
    """
    side = eth_side.lower()

    if btc_df is None or len(btc_df) < 21:
        logger.info("BTC 데이터 부족 (%s봉) → ETH 진입 허용",
                     0 if btc_df is None else len(btc_df))
        return True, "데이터부족"

    btc_df = ensure_indicators(btc_df)
    last = btc_df.iloc[-1]
    ema9: float = last["ema9"]
    ema21: float = last["ema21"]

    if ema21 == 0:
        return True, "데이터부족"

    diff_ratio = (ema9 - ema21) / ema21

    # flat: 차이 < 0.1%
    if abs(diff_ratio) < FLAT_THRESHOLD:
        return True, f"BTC flat (diff={diff_ratio:.4%})"

    # BTC 상승 → ETH SHORT 차단
    if diff_ratio > 0:
        if side == "short":
            reason = f"BTC 상승세 (EMA9>EMA21, diff={diff_ratio:.4%}) → ETH SHORT 차단"
            logger.info(reason)
            return False, reason
        return True, f"BTC 상승세 → ETH LONG 허용"

    # BTC 하락 → ETH LONG 차단
    if side == "long":
        reason = f"BTC 하락세 (EMA9<EMA21, diff={diff_ratio:.4%}) → ETH LONG 차단"
        logger.info(reason)
        return False, reason
    return True, f"BTC 하락세 → ETH SHORT 허용"
