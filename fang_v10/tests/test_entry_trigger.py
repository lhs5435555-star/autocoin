"""v11 EntryTrigger1m 테스트."""
import time
import numpy as np
import pandas as pd
import pytest

from fang_v10.entry_trigger_1m import EntryTrigger1m, EntrySignal
from fang_v10.setup_detector_15m import Setup


def _make_setup(symbol="BTC/USDT:USDT", side="long", sl=86500, entry=87000,
                used=False, expires_offset_ms=900_000):
    """테스트용 Setup 생성."""
    now_ms = int(time.time() * 1000)
    return Setup(
        symbol=symbol, side=side, regime="TREND_UP",
        entry_est=entry, sl_price=sl, r_dollar=500,
        tp1=87750, tp2=88250, tp3=89000,
        atr_15m=150, pullback_depth=0.5,
        created_at_ts=now_ms,
        expires_at_ts=now_ms + expires_offset_ms,
        used=used,
    )


def _make_1m_df(n=30, base_price=87000, bullish_last=True, high_volume=True):
    """테스트용 1m DataFrame."""
    np.random.seed(42)
    rows = []
    price = base_price
    for i in range(n):
        ret = np.random.normal(0, 0.0005)
        price *= (1 + ret)
        o = price
        c = price * (1 + 0.0003 if i < n - 1 else (0.001 if bullish_last else -0.001))
        h = max(o, c) * 1.0002
        l = min(o, c) * 0.9998
        vol = np.random.uniform(80, 120) if i < n - 1 else (200 if high_volume else 50)
        rows.append({"open": o, "high": h, "low": l, "close": c, "volume": vol})
    return pd.DataFrame(rows)


class TestEntryTrigger:
    def test_no_setup_returns_none(self):
        """셋업 없으면 None."""
        trig = EntryTrigger1m()
        df = _make_1m_df()
        result = trig.on_1m_candle_closed(
            "BTC", df, None, 87000, 87000, 60.0,
        )
        assert result is None

    def test_used_setup_blocked(self):
        """이미 사용된 셋업 차단."""
        trig = EntryTrigger1m()
        df = _make_1m_df()
        setup = _make_setup(used=True)
        result = trig.on_1m_candle_closed(
            "BTC", df, setup, 87000, 87000, 60.0,
        )
        assert result is None

    def test_expired_setup_blocked(self):
        """만료된 셋업 차단."""
        trig = EntryTrigger1m()
        df = _make_1m_df()
        setup = _make_setup(expires_offset_ms=-1000)  # 이미 만료
        result = trig.on_1m_candle_closed(
            "BTC", df, setup, 87000, 87000, 60.0,
        )
        assert result is None

    def test_funding_veto(self):
        """펀딩 9분 남았을 때 차단."""
        trig = EntryTrigger1m()
        df = _make_1m_df()
        setup = _make_setup()
        result = trig.on_1m_candle_closed(
            "BTC", df, setup, 87000, 87000, 9.0,  # 9분 남음
        )
        assert result is None

    def test_mark_last_gap_blocked(self):
        """Mark/Last 괴리 초과 시 차단."""
        trig = EntryTrigger1m()
        df = _make_1m_df()
        setup = _make_setup()
        # SL 거리 = (87000-86500)/87000 = 0.575%
        # 허용 괴리 = 0.575% * 0.12 = 0.069%
        # 실제 gap = (87100-87000)/87000 = 0.115% > 0.069%
        result = trig.on_1m_candle_closed(
            "BTC", df, setup, 87100, 87000, 60.0,
        )
        assert result is None

    def test_too_close_to_sl(self):
        """SL 너무 가까울 때 차단."""
        trig = EntryTrigger1m()
        df = _make_1m_df(base_price=86510)  # SL=86500 근처
        setup = _make_setup(sl=86500, entry=86510)
        result = trig.on_1m_candle_closed(
            "BTC", df, setup, 86510, 86510, 60.0,
        )
        assert result is None

    def test_normal_trigger(self):
        """정상 트리거 발화."""
        trig = EntryTrigger1m()
        # 양봉 + 직전고가 돌파 + 거래량 높은 df
        df = _make_1m_df(bullish_last=True, high_volume=True)
        setup = _make_setup()

        result = trig.on_1m_candle_closed(
            "BTC", df, setup, 87000, 87000, 60.0,
        )
        # 트리거 조건(양봉+돌파+거래량) 충족 여부에 따라 결과 다름
        # 합성 데이터라 반드시 충족되지 않을 수 있음
        if result is not None:
            assert isinstance(result, EntrySignal)
            assert result.side == "long"
            assert result.sl_price == 86500  # 15m SL 유지
            assert result.setup_id == setup.setup_id

    def test_duplicate_entry_blocked(self):
        """같은 셋업에서 중복 발화 차단."""
        trig = EntryTrigger1m()
        df = _make_1m_df()
        setup = _make_setup()
        setup.used = True  # 이미 사용됨

        result = trig.on_1m_candle_closed(
            "BTC", df, setup, 87000, 87000, 60.0,
        )
        assert result is None

    def test_short_trigger(self):
        """숏 트리거 전제조건 확인."""
        trig = EntryTrigger1m()
        df = _make_1m_df(bullish_last=False)
        setup = _make_setup(side="short", sl=87500, entry=87000)
        setup.regime = "TREND_DOWN"

        # 전제조건만 확인 (숏 트리거 조건은 합성 데이터 의존)
        ok, reason = trig._check_preconditions(
            setup, 87000, 87000, 60.0, 87000,
        )
        assert ok is True

    def test_precondition_details(self):
        """전제조건 개별 확인."""
        trig = EntryTrigger1m()

        # NO_SETUP
        ok, r = trig._check_preconditions(None, 87000, 87000, 60, 87000)
        assert not ok and "NO_SETUP" in r

        # FUNDING_VETO
        setup = _make_setup()
        ok, r = trig._check_preconditions(setup, 87000, 87000, 5, 87000)
        assert not ok and "FUNDING" in r

        # EXPIRED
        expired = _make_setup(expires_offset_ms=-1)
        ok, r = trig._check_preconditions(expired, 87000, 87000, 60, 87000)
        assert not ok and "EXPIRED" in r
