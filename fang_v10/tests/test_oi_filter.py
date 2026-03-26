"""v11 OI 필터 + Funding 함수 테스트."""
import time
import pytest
from fang_v10.oi_filter import OIFilter, get_minutes_to_funding


class TestOIFilter:
    def test_backtest_always_true(self):
        """백테스트 모드에서 항상 True."""
        f = OIFilter(backtest_mode=True)
        assert f.is_bullish("BTC") is True
        assert f.is_bearish("BTC") is True

    def test_not_ready_always_true(self):
        """30개 미만이면 항상 True."""
        f = OIFilter()
        for i in range(20):
            f.update("BTC", 100000 + i)
        assert not f.is_ready("BTC")
        assert f.is_bullish("BTC") is True
        assert f.is_bearish("BTC") is True

    def test_ready_after_30(self):
        """30개 이상이면 활성화."""
        f = OIFilter()
        for i in range(35):
            f.update("BTC", 100000 + i * 100)
        assert f.is_ready("BTC")

    def test_bullish_rising_oi(self):
        """OI 상승 → is_bullish True."""
        f = OIFilter()
        # 꾸준히 상승하는 OI
        for i in range(40):
            f.update("BTC", 100000 + i * 500)
        assert f.is_ready("BTC")
        assert f.is_bullish("BTC") is True

    def test_bearish_falling_oi(self):
        """OI 하락 → is_bearish True."""
        f = OIFilter()
        for i in range(40):
            f.update("BTC", 200000 - i * 500)
        assert f.is_ready("BTC")
        assert f.is_bearish("BTC") is True

    def test_bullish_false_on_falling(self):
        """OI 하락 → is_bullish False."""
        f = OIFilter()
        for i in range(40):
            f.update("BTC", 200000 - i * 500)
        assert f.is_bullish("BTC") is False

    def test_get_status(self):
        """디버그 상태."""
        f = OIFilter()
        for i in range(35):
            f.update("BTC", 100000 + i * 100)
        status = f.get_status("BTC")
        assert status["ready"] is True
        assert status["count"] == 35
        assert "ema6" in status

    def test_separate_symbols(self):
        """심볼별 독립."""
        f = OIFilter()
        for i in range(35):
            f.update("BTC", 100000 + i * 100)
        assert f.is_ready("BTC")
        assert not f.is_ready("ETH")
        assert f.is_bullish("ETH") is True  # 미준비 = 통과


class TestFunding:
    def test_minutes_to_funding(self):
        """다음 펀딩까지 남은 분."""
        minutes = get_minutes_to_funding()
        assert 0 < minutes <= 480  # 최대 8시간 = 480분

    def test_funding_positive(self):
        """항상 양수."""
        for _ in range(5):
            m = get_minutes_to_funding()
            assert m > 0

    def test_funding_within_8h(self):
        """8시간 이내."""
        m = get_minutes_to_funding()
        assert m <= 480.1
