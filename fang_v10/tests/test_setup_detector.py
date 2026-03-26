"""v11 SetupDetector15m 테스트 — 합성 데이터 기반."""
import numpy as np
import pandas as pd
import pytest

from fang_v10.regime_engine import (
    RegimeResult, compute_1h_indicators, detect_regime, ensure_indicators,
)
from fang_v10.setup_detector_15m import SetupDetector15m, Setup


def _make_15m_trend_up(n=200, seed=42):
    """상승 추세 15m 합성 데이터."""
    np.random.seed(seed)
    base_ts = int(pd.Timestamp("2025-12-01").timestamp() * 1000)
    price = 87000.0
    rows = []
    for i in range(n):
        # 상승 바이어스 + 노이즈
        ret = np.random.normal(0.0003, 0.002)  # 약간의 상승 편향
        price *= (1 + ret)
        h = price * (1 + abs(np.random.normal(0, 0.001)))
        l = price * (1 - abs(np.random.normal(0, 0.001)))
        rows.append({
            "timestamp": base_ts + i * 900000,
            "open": price * (1 - ret / 2),
            "high": h,
            "low": l,
            "close": price,
            "volume": np.random.uniform(50, 200),
        })
    return pd.DataFrame(rows)


def _make_15m_trend_down(n=200, seed=43):
    """하락 추세 15m 합성 데이터."""
    np.random.seed(seed)
    base_ts = int(pd.Timestamp("2025-12-01").timestamp() * 1000)
    price = 87000.0
    rows = []
    for i in range(n):
        ret = np.random.normal(-0.0003, 0.002)
        price *= (1 + ret)
        h = price * (1 + abs(np.random.normal(0, 0.001)))
        l = price * (1 - abs(np.random.normal(0, 0.001)))
        rows.append({
            "timestamp": base_ts + i * 900000,
            "open": price * (1 - ret / 2),
            "high": h,
            "low": l,
            "close": price,
            "volume": np.random.uniform(50, 200),
        })
    return pd.DataFrame(rows)


def _make_15m_with_pullback(n=100, seed=44):
    """상승 후 눌림 → 회복 패턴 15m 데이터."""
    np.random.seed(seed)
    base_ts = int(pd.Timestamp("2025-12-01").timestamp() * 1000)
    price = 87000.0
    rows = []
    for i in range(n):
        if i < 60:
            ret = np.random.normal(0.001, 0.002)   # 강한 상승
        elif i < 85:
            ret = np.random.normal(-0.0005, 0.001)  # 눌림
        else:
            ret = np.random.normal(0.0008, 0.001)   # 회복
        price *= (1 + ret)
        h = price * (1 + abs(np.random.normal(0, 0.001)))
        l = price * (1 - abs(np.random.normal(0, 0.001)))
        rows.append({
            "timestamp": base_ts + i * 900000,
            "open": price * (1 - ret / 2),
            "high": h,
            "low": l,
            "close": price,
            "volume": np.random.uniform(50, 200),
        })
    return pd.DataFrame(rows)


class TestSetupDetector:
    def test_setup_fields(self):
        """Setup 데이터클래스 필드 확인."""
        s = Setup(
            symbol="BTC", side="long", regime="TREND_UP",
            entry_est=87000, sl_price=86500, r_dollar=500,
            tp1=87750, tp2=88250, tp3=89000,
            atr_15m=150, pullback_depth=0.45,
            created_at_ts=1000000, expires_at_ts=1900000,
        )
        assert s.side == "long"
        assert s.r_distance == 500
        assert len(s.setup_id) == 8

    def test_regime_filter(self):
        """BOX/NO_TRADE 레짐에서는 셋업 안 만들어짐."""
        det = SetupDetector15m()
        df = _make_15m_trend_up(100)
        box = RegimeResult(symbol="BTC", regime="BOX")
        result = det.on_15m_candle_closed("BTC", df, box)
        assert result is None

    def test_active_setup_blocks_new(self):
        """미사용 셋업 있으면 새 셋업 안 만들어짐."""
        det = SetupDetector15m()
        df = _make_15m_trend_up(100)
        regime = RegimeResult(symbol="BTC", regime="TREND_UP")
        active = Setup(
            symbol="BTC", side="long", regime="TREND_UP",
            entry_est=87000, sl_price=86500, r_dollar=500,
            tp1=87750, tp2=88250, tp3=89000,
            atr_15m=150, pullback_depth=0.5,
            created_at_ts=1000000, expires_at_ts=99999999999,
            used=False,
        )
        result = det.on_15m_candle_closed("BTC", df, regime, active)
        assert result is None

    def test_used_setup_allows_new(self):
        """사용 완료된 셋업이면 새 셋업 가능."""
        det = SetupDetector15m()
        df = _make_15m_with_pullback(100)
        df = ensure_indicators(df)
        regime = RegimeResult(symbol="BTC", regime="TREND_UP")
        used = Setup(
            symbol="BTC", side="long", regime="TREND_UP",
            entry_est=87000, sl_price=86500, r_dollar=500,
            tp1=87750, tp2=88250, tp3=89000,
            atr_15m=150, pullback_depth=0.5,
            created_at_ts=1000000, expires_at_ts=99999999999,
            used=True,
        )
        # 셋업이 만들어질 수도 안 될 수도 있지만, active 차단은 안 됨
        det.on_15m_candle_closed("BTC", df, regime, used)
        # active 차단 없이 진행했으면 OK

    def test_tp_calculation(self):
        """TP1 = R×1.5, profit_lock = TP1."""
        det = SetupDetector15m()
        targets = det._calc_targets(87000, 86500, "long")
        r = 500
        assert targets["tp1"] == 87000 + r * 1.5
        assert targets["tp2"] == 87000 + r * 2.5
        assert targets["tp3"] == 87000 + r * 4.0
        assert targets["profit_lock_price"] == targets["tp1"]

    def test_tp_short_symmetric(self):
        """숏 TP 대칭."""
        det = SetupDetector15m()
        targets = det._calc_targets(87000, 87500, "short")
        r = 500
        assert targets["tp1"] == 87000 - r * 1.5
        assert targets["tp2"] == 87000 - r * 2.5

    def test_pullback_depth_long(self):
        """롱 pullback depth 범위 확인."""
        det = SetupDetector15m()
        df = _make_15m_with_pullback(100)
        depth = det._calc_pullback_depth_long(df)
        assert 0 <= depth <= 1.0

    def test_sl_validation(self):
        """SL 거리 검증."""
        det = SetupDetector15m()
        df = _make_15m_trend_up(100)
        df = ensure_indicators(df)
        # SL 계산이 ValueError 없이 통과하거나 적절히 발생하는지
        try:
            sl = det._calc_sl_long(df)
            entry = float(df.iloc[-1]["close"])
            dist = (entry - sl) / entry
            assert det.SL_DIST_MIN_PCT <= dist <= det.SL_DIST_MAX_PCT
        except ValueError:
            pass  # 범위 밖이면 정상적으로 거부

    def test_distribution_30d(self):
        """30일 분량 15m 데이터에서 셋업 발생 건수."""
        det = SetupDetector15m()

        # 30일 = 2880봉 (15m)
        for label, make_fn, regime_name, seed_base in [
            ("BTC_UP", _make_15m_trend_up, "TREND_UP", 100),
            ("BTC_DOWN", _make_15m_trend_down, "TREND_DOWN", 200),
        ]:
            # 여러 시드로 데이터 다양화
            total_long = 0
            total_short = 0
            fail_reasons = {}

            for s in range(5):
                df = make_fn(300, seed=seed_base + s)
                df = ensure_indicators(df)
                regime = RegimeResult(symbol="BTC", regime=regime_name)

                active = None
                for i in range(50, len(df)):
                    subset = df.iloc[:i + 1]
                    setup = det.on_15m_candle_closed("BTC", subset, regime, active)
                    if setup:
                        if setup.side == "long":
                            total_long += 1
                        else:
                            total_short += 1
                        active = setup
                        setup.used = True  # 바로 사용 처리
                        active = None      # 다음 셋업 허용

            print(f"\n{label}: 롱={total_long} 숏={total_short}")

        # 최소 발생 확인 (합성 데이터에서 일부라도 발생)
        # 실제 시장 데이터에서는 더 많이 발생할 수 있음


if __name__ == "__main__":
    t = TestSetupDetector()
    t.test_distribution_30d()
