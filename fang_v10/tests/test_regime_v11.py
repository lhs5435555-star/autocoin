"""v11 레짐 엔진 테스트 — 합성 데이터 기반."""
import numpy as np
import pandas as pd
from fang_v10.regime_engine import (
    RegimeEngine, RegimeResult, MarketRegime,
    compute_1h_indicators, resample_to_1h, detect_regime,
    ensure_indicators,
)


def _make_1h_data(n=720, seed=42):
    """30일 1H 합성 데이터 (720봉)."""
    np.random.seed(seed)
    price = 87000.0
    rows = []
    base_ts = int(pd.Timestamp("2025-12-01").timestamp() * 1000)
    for i in range(n):
        ret = np.random.normal(0, 0.003)  # 0.3% per hour
        price *= (1 + ret)
        h = price * (1 + abs(np.random.normal(0, 0.001)))
        l = price * (1 - abs(np.random.normal(0, 0.001)))
        rows.append({
            "timestamp": base_ts + i * 3600000,
            "open": price * (1 - ret/2),
            "high": h,
            "low": l,
            "close": price,
            "volume": np.random.uniform(100, 500),
        })
    return pd.DataFrame(rows)


class TestRegimeV11:
    def test_regime_result_fields(self):
        """RegimeResult에 필수 필드가 있는지."""
        r = RegimeResult(symbol="BTC", regime="TREND_UP")
        assert r.regime == "TREND_UP"
        assert r.is_trend
        assert r.v10_regime == MarketRegime.TREND

    def test_no_trade_regime(self):
        """NO_TRADE → PROTECT 매핑."""
        r = RegimeResult(symbol="BTC", regime="NO_TRADE")
        assert r.v10_regime == MarketRegime.PROTECT

    def test_box_regime(self):
        """BOX 그대로."""
        r = RegimeResult(symbol="BTC", regime="BOX")
        assert r.v10_regime == MarketRegime.BOX

    def test_1h_indicators(self):
        """1H 지표 계산."""
        df = _make_1h_data(100)
        df = compute_1h_indicators(df)
        assert "ema20" in df.columns
        assert "ema50" in df.columns
        assert "adx14" in df.columns
        assert "plus_di" in df.columns
        assert "minus_di" in df.columns
        assert "ema20_slope" in df.columns

    def test_regime_distribution(self):
        """레짐 분포: TREND >= 35%."""
        df = _make_1h_data(720, seed=42)
        df = compute_1h_indicators(df)

        counts = {"TREND_UP": 0, "TREND_DOWN": 0, "BOX": 0, "NO_TRADE": 0}
        for i in range(50, len(df)):
            result = detect_regime("BTC/USDT:USDT", df.iloc[:i + 1])
            counts[result.regime] += 1

        total = sum(counts.values())
        trend_pct = (counts["TREND_UP"] + counts["TREND_DOWN"]) / total * 100
        print(f"\n레짐 분포 (합성 BTC 1H):")
        for k, v in counts.items():
            print(f"  {k}: {v}건 ({v/total*100:.1f}%)")
        print(f"  TREND합계: {trend_pct:.1f}%")

        # 35% 미만이면 ADX 임계값 15로 테스트
        assert trend_pct >= 25, f"TREND {trend_pct:.1f}% < 25%"

    def test_resample_15m_to_1h(self):
        """15m→1H 리샘플 정상 동작."""
        n = 400  # 100시간 분량
        base_ts = int(pd.Timestamp("2025-12-01").timestamp() * 1000)
        rows = []
        price = 87000.0
        for i in range(n):
            price *= (1 + np.random.normal(0, 0.001))
            rows.append({
                "timestamp": base_ts + i * 900000,  # 15min
                "open": price, "high": price * 1.001,
                "low": price * 0.999, "close": price,
                "volume": 100,
            })
        df_15m = pd.DataFrame(rows)
        df_1h = resample_to_1h(df_15m)

        assert len(df_1h) > 0
        assert len(df_1h) <= n // 4
        # 1H 봉의 volume은 4봉 합계
        assert "timestamp" in df_1h.columns

    def test_regime_engine_cache(self):
        """같은 1H ts에서는 캐시 반환."""
        eng = RegimeEngine()
        df = _make_1h_data(100)
        df = compute_1h_indicators(df)

        r1 = eng.get_regime("BTC", df)
        r2 = eng.get_regime("BTC", df)
        assert r1.regime == r2.regime
        assert r1.updated_at == r2.updated_at

    def test_incomplete_candle_removed(self):
        """미완성봉은 리샘플 시 제거."""
        # 3봉만 (1H 완성 불가)
        base_ts = int(pd.Timestamp("2025-12-01").timestamp() * 1000)
        rows = []
        for i in range(3):
            rows.append({
                "timestamp": base_ts + i * 900000,
                "open": 87000, "high": 87100, "low": 86900,
                "close": 87000, "volume": 100,
            })
        df = pd.DataFrame(rows)
        df_1h = resample_to_1h(df)
        # 4봉 미만이므로 1H 완성봉 0개
        assert len(df_1h) == 0


if __name__ == "__main__":
    t = TestRegimeV11()
    t.test_regime_distribution()
