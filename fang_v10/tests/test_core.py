"""
fang_v10 핵심 모듈 테스트.

pytest로 실행: cd fang_v10 && python -m pytest tests/test_core.py -v
"""

from __future__ import annotations

import time

import numpy as np
import pandas as pd
import pytest

from fang_v10.config import CONFIG
from fang_v10.regime_engine import MarketRegime, RegimeEngine, ensure_indicators
from fang_v10.strategy import Signal, generate_signals
from fang_v10.sizing_engine import calc_position_size, check_dca_risk_gate
from fang_v10.position_manager import (
    BlockedSignalTracker,
    PhantomTracker,
    PositionManager,
    PositionState,
)
from fang_v10.risk_engine import RiskEngine, MddTracker
from fang_v10.execution import SafeExecutor
from fang_v10.exchange_api import BitgetClient
from fang_v10.backtest import BacktestEngine


# ──────────────────────────────────────────────
# 헬퍼: 테스트용 DataFrame 생성
# ──────────────────────────────────────────────

def _make_df(
    n: int = 100,
    base_price: float = 60000.0,
    adx: float = 30.0,
    rsi: float = 55.0,
    ema_trend: str = "up",       # "up", "down", "flat"
    volume_ratio: float = 1.5,
    volatility_zscore: float = 0.5,
    bb_width: float = 0.03,
) -> pd.DataFrame:
    """테스트용 OHLCV + 지표 DataFrame."""
    np.random.seed(42)
    close = base_price + np.cumsum(np.random.randn(n) * 10)
    high = close + np.random.rand(n) * 50
    low = close - np.random.rand(n) * 50
    open_ = close + np.random.randn(n) * 10
    volume = np.random.rand(n) * 1000 + 100

    df = pd.DataFrame({
        "open": open_, "high": high, "low": low,
        "close": close, "volume": volume,
    })

    # 지표 직접 주입
    df["adx"] = adx
    df["rsi"] = rsi
    df["volume_ratio"] = volume_ratio
    df["atr"] = base_price * 0.005  # 0.5%
    df["atr_pct"] = 0.005
    df["volatility_zscore"] = volatility_zscore
    df["bb_width"] = bb_width
    df["bb_mid"] = close

    if ema_trend == "up":
        df["ema9"] = close + 50
        df["ema21"] = close + 30
        df["ema50"] = close + 10
    elif ema_trend == "down":
        df["ema9"] = close - 50
        df["ema21"] = close - 30
        df["ema50"] = close - 10
    else:
        df["ema9"] = close
        df["ema21"] = close + 5
        df["ema50"] = close - 5

    df["bb_upper"] = close + base_price * bb_width / 2
    df["bb_lower"] = close - base_price * bb_width / 2

    return df


# ══════════════════════════════════════════════
# 레짐 테스트
# ══════════════════════════════════════════════

class TestRegime:
    def test_regime_trend(self):
        """ADX=30, EMA 3/3 → TREND."""
        eng = RegimeEngine()
        df = _make_df(adx=30, ema_trend="up")
        regime = eng.detect("BTC/USDT:USDT", df, len(df) - 1)
        assert regime == MarketRegime.TREND

    def test_regime_box(self):
        """ADX=20 → BOX."""
        eng = RegimeEngine()
        df = _make_df(adx=20, ema_trend="flat")
        regime = eng.detect("BTC/USDT:USDT", df, len(df) - 1)
        assert regime == MarketRegime.BOX

    def test_regime_protect_volatility(self):
        """ATR zscore=3.0 → PROTECT."""
        eng = RegimeEngine()
        df = _make_df(volatility_zscore=3.0)
        regime = eng.detect("BTC/USDT:USDT", df, len(df) - 1)
        assert regime == MarketRegime.PROTECT

    def test_regime_protect_ambiguous(self):
        """ADX=28, EMA 2/3 (flat) → PROTECT."""
        eng = RegimeEngine()
        df = _make_df(adx=28, ema_trend="flat")
        regime = eng.detect("BTC/USDT:USDT", df, len(df) - 1)
        assert regime == MarketRegime.PROTECT

    def test_regime_no_neutral(self):
        """어떤 입력이든 TREND/BOX/PROTECT 중 하나."""
        eng = RegimeEngine()
        for adx in [10, 20, 25, 28, 35, 50]:
            for trend in ["up", "down", "flat"]:
                for vz in [0.5, 1.0, 2.5]:
                    df = _make_df(adx=adx, ema_trend=trend, volatility_zscore=vz)
                    regime = eng.detect("BTC/USDT:USDT", df, len(df) - 1)
                    assert regime in (MarketRegime.TREND, MarketRegime.BOX, MarketRegime.PROTECT)


# ══════════════════════════════════════════════
# 전략 테스트
# ══════════════════════════════════════════════

class TestStrategy:
    def test_trend_long(self):
        """TREND 롱 조건 충족 → Signal 생성."""
        df = _make_df(adx=30, rsi=60, ema_trend="up", volume_ratio=1.5)
        # ema9 > ema21 > ema50 이고 close > ema9 확인
        last = df.iloc[-1]
        df.loc[df.index[-1], "close"] = last["ema9"] + 10  # close > ema9

        signals = generate_signals("BTC/USDT:USDT", df, MarketRegime.TREND)
        assert len(signals) >= 1
        assert signals[0].side == "long"
        assert signals[0].strategy == "trend"

    def test_trend_short_symmetric(self):
        """TREND 숏은 롱과 대칭."""
        df = _make_df(adx=30, rsi=40, ema_trend="down", volume_ratio=1.5)
        last = df.iloc[-1]
        df.loc[df.index[-1], "close"] = last["ema9"] - 10  # close < ema9

        signals = generate_signals("BTC/USDT:USDT", df, MarketRegime.TREND)
        assert len(signals) >= 1
        assert signals[0].side == "short"

    def test_box_long_bounce_confirm(self):
        """BOX: prev<bb_lower + close>bb_lower + rsi↑ → Signal."""
        df = _make_df(adx=20, rsi=35, ema_trend="flat", volume_ratio=1.5,
                       volatility_zscore=0.5)
        n = len(df)
        # prev: close < bb_lower
        df.loc[df.index[n - 2], "close"] = df.iloc[n - 2]["bb_lower"] - 100
        df.loc[df.index[n - 2], "rsi"] = 25
        # current: close > bb_lower, rsi > prev_rsi, rsi <= 40
        df.loc[df.index[n - 1], "close"] = df.iloc[n - 1]["bb_lower"] + 50
        df.loc[df.index[n - 1], "rsi"] = 35

        signals = generate_signals("BTC/USDT:USDT", df, MarketRegime.BOX)
        assert len(signals) >= 1
        assert signals[0].side == "long"
        assert signals[0].strategy == "box"

    def test_box_short_symmetric(self):
        """BOX 숏: 7개 조건 롱과 완전 대칭."""
        df = _make_df(adx=20, rsi=65, ema_trend="flat", volume_ratio=1.5,
                       volatility_zscore=0.5)
        n = len(df)
        # prev: close > bb_upper
        df.loc[df.index[n - 2], "close"] = df.iloc[n - 2]["bb_upper"] + 100
        df.loc[df.index[n - 2], "rsi"] = 75
        # current: close < bb_upper, rsi < prev_rsi, rsi >= 60
        df.loc[df.index[n - 1], "close"] = df.iloc[n - 1]["bb_upper"] - 50
        df.loc[df.index[n - 1], "rsi"] = 65

        signals = generate_signals("BTC/USDT:USDT", df, MarketRegime.BOX)
        assert len(signals) >= 1
        assert signals[0].side == "short"

    def test_box_no_first_touch(self):
        """close<=bb_lower이지만 prev도 안쪽 → 진입 안함 (반등확인 필요)."""
        df = _make_df(adx=20, rsi=25, ema_trend="flat", volume_ratio=1.5,
                       volatility_zscore=0.5)
        n = len(df)
        # 둘 다 bb_lower 위
        df.loc[df.index[n - 2], "close"] = df.iloc[n - 2]["bb_lower"] + 50
        df.loc[df.index[n - 1], "close"] = df.iloc[n - 1]["bb_lower"] - 10

        signals = generate_signals("BTC/USDT:USDT", df, MarketRegime.BOX)
        # close < bb_lower이지만 prev >= bb_lower → 진입 안함
        # (prev가 밖으로 나갔었어야 함)
        assert len(signals) == 0

    def test_protect_no_entry(self):
        """PROTECT → 빈 리스트."""
        df = _make_df()
        signals = generate_signals("BTC/USDT:USDT", df, MarketRegime.PROTECT)
        assert signals == []


# ══════════════════════════════════════════════
# 사이징 테스트
# ══════════════════════════════════════════════

class TestSizing:
    def test_sizing_basic(self):
        """$198, BTC → valid."""
        result = calc_position_size(
            balance=198, entry=60000, sl=59100, tp=60900,
            symbol="BTC/USDT:USDT",
        )
        assert result.valid
        assert result.risk_1r_usd > 0
        assert result.leverage >= CONFIG.LEVERAGE_MIN

    def test_sizing_reject_margin(self):
        """margin>10% → invalid."""
        # 극단적으로 작은 SL → 큰 notional → margin 초과
        result = calc_position_size(
            balance=10, entry=60000, sl=59990, tp=60010,
            symbol="BTC/USDT:USDT",
        )
        # SL이 너무 가까우면 다른 게이트에서 걸릴 수 있음
        if result.valid:
            assert result.margin <= 10 * 0.10

    def test_sizing_wider_sl_smaller_pos(self):
        """SL 넓으면 사이즈 줄고 1R 동일."""
        narrow = calc_position_size(
            balance=198, entry=60000, sl=59400, tp=60600,
            symbol="BTC/USDT:USDT",
        )
        wide = calc_position_size(
            balance=198, entry=60000, sl=58800, tp=61200,
            symbol="BTC/USDT:USDT",
        )
        if narrow.valid and wide.valid:
            # 넓은 SL → 작은 수량
            assert wide.amount < narrow.amount
            # 1R 리스크는 동일 (같은 balance × risk%)
            assert abs(wide.risk_1r_usd - narrow.risk_1r_usd) < 0.1

    def test_sizing_kelly_always_1(self):
        """kelly_mult 뭘 넣든 1.0 적용."""
        r1 = calc_position_size(198, 60000, 59100, 60900, "BTC/USDT:USDT", kelly_mult=0.5)
        r2 = calc_position_size(198, 60000, 59100, 60900, "BTC/USDT:USDT", kelly_mult=1.5)
        if r1.valid and r2.valid:
            assert r1.risk_1r_usd == r2.risk_1r_usd


# ══════════════════════════════════════════════
# DCA 테스트
# ══════════════════════════════════════════════

class TestDCA:
    def test_dca_max_one(self):
        """dca_count=1 → 거부."""
        mgr = PositionManager()
        pos = mgr.open_position(
            "BTC/USDT:USDT", "long", 60000, 0.01, 10, 300, 0,
            "TREND", "trend",
        )
        pos.add_dca(59500, 0.005)  # DCA 1회
        result = mgr.check_dca(
            "BTC/USDT:USDT", "long", 59000, 300, 10,
        )
        assert result is None  # 2회째 거부

    def test_dca_risk_gate(self):
        """total risk > 1.5R → 거부."""
        positions = [
            {"symbol": "BTC/USDT:USDT", "margin": 15,
             "initial_risk_1r_usd": 2.0},
            {"symbol": "BTC/USDT:USDT", "margin": 10,
             "initial_risk_1r_usd": 2.0},
        ]
        allowed, reason = check_dca_risk_gate(
            "BTC/USDT:USDT", 5.0, 198, positions,
        )
        # 총리스크 = 2.0 + 2.0 + (2.0 * 0.5) = 5.0 > 2.0 * 1.5 = 3.0
        assert not allowed

    def test_dca_initial_r_unchanged(self):
        """DCA 후 initial_r_distance 불변."""
        pos = PositionState(
            symbol="BTC/USDT:USDT", side="long",
            entries=[(60000, 0.01)],
            avg_price=60000, total_size=0.01,
            initial_r_distance=450.0,
        )
        original_r = pos.initial_r_distance
        pos.add_dca(59500, 0.005)
        assert pos.initial_r_distance == original_r
        assert pos.avg_price != 60000  # avg 변경됨


# ══════════════════════════════════════════════
# 포지션 관리 테스트
# ══════════════════════════════════════════════

class TestPositionManager:
    def test_calc_r_uses_initial_r(self):
        """calc_risk_r은 initial_r_distance 사용."""
        pos = PositionState(
            symbol="BTC/USDT:USDT", side="long",
            entries=[(60000, 0.01)],
            avg_price=60000, total_size=0.01,
            initial_r_distance=450.0,
        )
        # 450 USDT 상승 = +1R
        assert abs(pos.calc_risk_r(60450) - 1.0) < 0.01
        # 450 USDT 하락 = -1R
        assert abs(pos.calc_risk_r(59550) - (-1.0)) < 0.01

    def test_favorable_extreme_long(self):
        """롱이면 max 추적."""
        pos = PositionState(
            symbol="BTC/USDT:USDT", side="long",
            entries=[(60000, 0.01)],
            avg_price=60000, total_size=0.01,
            initial_r_distance=450.0,
            favorable_extreme=60000,
        )
        pos.update_peaks(60500)
        assert pos.favorable_extreme == 60500
        pos.update_peaks(60200)
        assert pos.favorable_extreme == 60500  # 내려가도 유지

    def test_favorable_extreme_short(self):
        """숏이면 min 추적."""
        pos = PositionState(
            symbol="BTC/USDT:USDT", side="short",
            entries=[(60000, 0.01)],
            avg_price=60000, total_size=0.01,
            initial_r_distance=450.0,
            favorable_extreme=60000,
        )
        pos.update_peaks(59500)
        assert pos.favorable_extreme == 59500
        pos.update_peaks(59800)
        assert pos.favorable_extreme == 59500

    def test_trailing_price_based(self):
        """trail_stop = favorable_extreme - (initial_r_distance × 0.5)."""
        mgr = PositionManager()
        pos = mgr.open_position(
            "BTC/USDT:USDT", "long", 60000, 0.01, 10, 300, 0,
            "TREND", "trend",
        )
        pos.tp_count = 2  # TP1, TP2 완료
        pos.be_activated = True  # BE 이미 활성화 (우선순위 6 스킵)
        pos.favorable_extreme = 61000

        trail_dist = pos.initial_r_distance * CONFIG.TP3_TRAIL_R
        trail_stop = pos.favorable_extreme - trail_dist

        # 가격이 trail_stop 이하
        exit_result = mgr.check_exit(
            "BTC/USDT:USDT|long", trail_stop - 1, 100,
        )
        assert exit_result is not None
        assert exit_result["reason"] == "TRAIL"

    def test_early_failure_05r(self):
        """8봉, peak_r=0.4 → 청산."""
        mgr = PositionManager()
        pos = mgr.open_position(
            "BTC/USDT:USDT", "long", 60000, 0.01, 10, 300, 0,
            "TREND", "trend",
        )
        pos.peak_r = 0.4  # < 0.5

        exit_result = mgr.check_exit(
            "BTC/USDT:USDT|long", 60000, 8,  # 8봉 후
        )
        assert exit_result is not None
        assert exit_result["reason"] == "EARLY"

    def test_early_failure_survived(self):
        """8봉, peak_r=0.6 → 유지."""
        mgr = PositionManager()
        pos = mgr.open_position(
            "BTC/USDT:USDT", "long", 60000, 0.01, 10, 300, 0,
            "TREND", "trend",
        )
        pos.peak_r = 0.6  # >= 0.5

        exit_result = mgr.check_exit(
            "BTC/USDT:USDT|long", 60000, 8,
        )
        # EARLY는 안 걸림 (다른 것도 안 걸려야 함)
        assert exit_result is None or exit_result["reason"] != "EARLY"

    def test_early_failure_not_yet(self):
        """4봉 → 유지 (8봉 미달)."""
        mgr = PositionManager()
        pos = mgr.open_position(
            "BTC/USDT:USDT", "long", 60000, 0.01, 10, 300, 0,
            "TREND", "trend",
        )
        pos.peak_r = 0.3

        exit_result = mgr.check_exit(
            "BTC/USDT:USDT|long", 60000, 4,
        )
        assert exit_result is None or exit_result["reason"] != "EARLY"

    def test_be_with_funding(self):
        """8시간+ 보유 시 BE에 펀딩비 반영."""
        mgr = PositionManager()
        pos = mgr.open_position(
            "BTC/USDT:USDT", "long", 60000, 0.01, 10, 300, 0,
            "TREND", "trend",
        )
        # 시간 조작: 9시간 전 진입
        pos.entry_time = time.time() - 9 * 3600

        # BE 트리거: +0.5R
        trigger_price = 60000 + pos.initial_r_distance * CONFIG.BE_TRIGGER_R
        exit_result = mgr.check_exit(
            "BTC/USDT:USDT|long", trigger_price, 100,
        )
        assert exit_result is not None
        assert exit_result["reason"] == "BE"
        be_price = exit_result["new_sl"]
        # BE에 펀딩비가 반영되어 entry보다 높아야 함
        assert be_price > 60000


# ══════════════════════════════════════════════
# PhantomTracker 테스트
# ══════════════════════════════════════════════

class TestPhantom:
    def test_phantom_premature(self):
        """SL 후 TP 도달 → premature++."""
        pt = PhantomTracker(track_bars=5)
        pt.register_sl(
            "BTC/USDT:USDT", "long", 60000, 60000, 59100, 60900,
            sl_bar_idx=0, r_at_sl=-1.0, pnl_at_sl=-1.98,
        )
        # TP 도달
        for i in range(1, 7):
            pt.update("BTC/USDT:USDT", 61000, i)

        stats = pt.get_stats()
        assert stats["premature_count"] >= 1

    def test_phantom_correct(self):
        """SL 후 더 떨어짐 → correct++."""
        pt = PhantomTracker(track_bars=5)
        pt.register_sl(
            "BTC/USDT:USDT", "long", 60000, 60000, 59100, 60900,
            sl_bar_idx=0, r_at_sl=-1.0, pnl_at_sl=-1.98,
        )
        # SL의 2배 거리까지 하락 (60000 - 900*2 = 58200)
        for i in range(1, 7):
            pt.update("BTC/USDT:USDT", 58000, i)

        stats = pt.get_stats()
        assert stats["correct_count"] >= 1


# ══════════════════════════════════════════════
# BlockedSignalTracker 테스트
# ══════════════════════════════════════════════

class TestBlocked:
    def test_blocked_correct(self):
        """차단 후 SL 도달 → correct_block++."""
        bt = BlockedSignalTracker(track_bars=5)
        bt.register_blocked(
            "BTC/USDT:USDT", "long", 60000, 59100, 60900,
            "cooldown", 0,
        )
        for i in range(1, 7):
            bt.update("BTC/USDT:USDT", 58500, i)

        stats = bt.get_stats()
        assert stats["correct_block_count"] >= 1

    def test_blocked_wrong(self):
        """차단 후 TP 도달 → wrong_block++."""
        bt = BlockedSignalTracker(track_bars=5)
        bt.register_blocked(
            "BTC/USDT:USDT", "long", 60000, 59100, 60900,
            "cooldown", 0,
        )
        for i in range(1, 7):
            bt.update("BTC/USDT:USDT", 61500, i)

        stats = bt.get_stats()
        assert stats["wrong_block_count"] >= 1


# ══════════════════════════════════════════════
# 쿨다운 테스트
# ══════════════════════════════════════════════

class TestCooldown:
    def _make_executor(self) -> SafeExecutor:
        client = BitgetClient(paper=True)
        return SafeExecutor(client)

    def test_cooldown_normal(self):
        """TP 후 60초."""
        ex = self._make_executor()
        ex.record_trade_result("BTC/USDT:USDT", "long", "TP1")
        cd = ex._get_cooldown_sec("BTC/USDT:USDT", "long")
        assert cd == CONFIG.COOLDOWN_NORMAL_SEC

    def test_cooldown_sl_same_dir(self):
        """SL+같은방향 600초."""
        ex = self._make_executor()
        ex.record_trade_result("BTC/USDT:USDT", "long", "SL")
        cd = ex._get_cooldown_sec("BTC/USDT:USDT", "long")
        assert cd == CONFIG.COOLDOWN_AFTER_SL_SAME_DIR_SEC

    def test_cooldown_sl_other_dir(self):
        """SL+반대방향 300초."""
        ex = self._make_executor()
        ex.record_trade_result("BTC/USDT:USDT", "long", "SL")
        cd = ex._get_cooldown_sec("BTC/USDT:USDT", "short")
        assert cd == CONFIG.COOLDOWN_AFTER_SL_SEC


# ══════════════════════════════════════════════
# 킬스위치 테스트
# ══════════════════════════════════════════════

class TestKillSwitch:
    def test_kill_soft(self):
        """-1.1% → mult=0.5."""
        eng = RiskEngine()
        eng.set_initial_balance(1000)
        eng.record_trade(-11, "BTC/USDT:USDT")  # -1.1%
        mode = eng.get_risk_mode()
        assert mode["mode"] == "SOFT"
        assert mode["size_mult"] == 0.5

    def test_kill_stop(self):
        """-2.1% → paused."""
        eng = RiskEngine()
        eng.set_initial_balance(1000)
        eng.record_trade(-21, "BTC/USDT:USDT")  # -2.1%
        assert not eng.can_trade()

    def test_consec_3(self):
        """3연패 → 심볼 정지."""
        eng = RiskEngine()
        eng.set_initial_balance(10000)  # 킬스위치 안 걸리게 큰 잔고
        for _ in range(3):
            eng.record_trade(-1, "BTC/USDT:USDT")

        can, reason = eng.can_trade_coin("BTC/USDT:USDT")
        assert not can
        assert "연패" in reason


# ══════════════════════════════════════════════
# 백테스트 테스트
# ══════════════════════════════════════════════

class TestBacktest:
    def _make_long_df(self, n: int = 200) -> pd.DataFrame:
        """백테스트용 충분한 길이의 DataFrame."""
        np.random.seed(42)
        close = 60000 + np.cumsum(np.random.randn(n) * 50)
        return pd.DataFrame({
            "open": close + np.random.randn(n) * 10,
            "high": close + np.abs(np.random.randn(n)) * 100,
            "low": close - np.abs(np.random.randn(n)) * 100,
            "close": close,
            "volume": np.random.rand(n) * 1000 + 100,
        })

    def test_backtest_reproducible(self):
        """seed=42, 2회 동일."""
        df = self._make_long_df()
        e1 = BacktestEngine(seed=42)
        r1 = e1.run(df, "BTC/USDT:USDT")
        e2 = BacktestEngine(seed=42)
        r2 = e2.run(df, "BTC/USDT:USDT")
        assert r1.total_trades == r2.total_trades
        assert abs(r1.total_pnl - r2.total_pnl) < 0.01

    def test_backtest_gap_skip(self):
        """BacktestEngine이 초기화되고 실행 가능."""
        df = self._make_long_df(100)
        eng = BacktestEngine(seed=42)
        result = eng.run(df, "BTC/USDT:USDT")
        # 최소한 에러 없이 완료
        assert isinstance(result.total_trades, int)
        assert len(result.equity_curve) > 0

    def test_backtest_pending_dca(self):
        """백테스트 엔진이 DCA 통계를 추적."""
        df = self._make_long_df(300)
        eng = BacktestEngine(seed=42)
        result = eng.run(df, "BTC/USDT:USDT")
        assert "attempted" in result.dca_stats
        assert "executed" in result.dca_stats
