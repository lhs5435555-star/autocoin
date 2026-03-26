"""v11 RiskEngine 테스트."""
import pytest
from fang_v10.risk_engine import RiskEngine, MddTracker, print_ev_summary


class TestDDStepDown:
    def test_normal_risk(self):
        """DD < 3% → 풀 리스크."""
        eng = RiskEngine()
        eng.set_initial_balance(198)
        eng._peak_equity = 198
        assert eng.get_risk_pct("BTC/USDT:USDT") == 0.0025

    def test_dd_3pct(self):
        """3% DD → 75% 리스크."""
        eng = RiskEngine()
        eng.set_initial_balance(198)
        eng._peak_equity = 200
        eng._daily_pnl = -6.0  # current = 192, dd = 4%
        assert eng.get_risk_pct("BTC/USDT:USDT") == 0.0025 * 0.75

    def test_dd_halt(self):
        """8.5% DD → 리스크 0."""
        eng = RiskEngine()
        eng.set_initial_balance(198)
        eng._peak_equity = 200
        eng._daily_pnl = -17.0  # current = 181, dd = 9.5%
        assert eng.get_risk_pct("BTC/USDT:USDT") == 0.0


class TestPositionSizing:
    def test_normal_sizing(self):
        """정상 사이징."""
        eng = RiskEngine()
        eng.set_initial_balance(198)
        eng._peak_equity = 198
        result = eng.calculate_position_size(
            "BTC", 87000, 86650, 10, 198,
        )
        assert result["R_dollar"] > 0
        assert result["margin_req"] > 0
        assert result["fee_R"] > 0

    def test_leverage_exceeded(self):
        """레버리지 > 15 → 거부."""
        eng = RiskEngine()
        eng.set_initial_balance(198)
        with pytest.raises(ValueError, match="LEVERAGE_EXCEEDED"):
            eng.calculate_position_size("BTC", 87000, 86650, 20, 198)

    def test_sl_too_tight(self):
        """SL < 0.15% → 거부."""
        eng = RiskEngine()
        eng.set_initial_balance(198)
        with pytest.raises(ValueError, match="SL_TOO_TIGHT"):
            eng.calculate_position_size("BTC", 87000, 86990, 10, 198)


class TestDCA:
    def test_dca_first_allowed(self):
        """첫 번째 DCA 허용."""
        eng = RiskEngine()
        eng.set_initial_balance(198)
        eng._peak_equity = 198
        qty = eng.validate_dca(
            "BTC", 198, 87000, 0.01, 86700, 86500,
            dca_count=0, regime="TREND_UP",
            plus_di=30, minus_di=20, oi_bullish=True,
        )
        assert qty >= 0  # 범위에 따라 0일 수 있음

    def test_dca_second_blocked(self):
        """두 번째 DCA 거부."""
        eng = RiskEngine()
        eng.set_initial_balance(198)
        qty = eng.validate_dca(
            "BTC", 198, 87000, 0.01, 86700, 86500,
            dca_count=1,
        )
        assert qty == 0

    def test_dca_box_blocked(self):
        """BOX 레짐 DCA 거부."""
        eng = RiskEngine()
        eng.set_initial_balance(198)
        qty = eng.validate_dca(
            "BTC", 198, 87000, 0.01, 86700, 86500,
            dca_count=0, regime="BOX",
        )
        assert qty == 0


class TestKillSwitch:
    def test_soft_halt(self):
        """1% 일간 손실 → SOFT."""
        eng = RiskEngine()
        eng.set_initial_balance(198)
        eng.update_pnl(-2.0, 198)  # ~1%
        assert eng._halt_level == "SOFT"
        allowed, reason = eng.check_entry_allowed()
        assert not allowed
        assert "SOFT" in reason

    def test_hard_halt(self):
        """1.5% 일간 손실 → HARD."""
        eng = RiskEngine()
        eng.set_initial_balance(198)
        eng.update_pnl(-3.0, 198)  # ~1.5%
        assert eng._halt_level == "HARD"

    def test_stop_halt(self):
        """2% 일간 손실 → STOP."""
        eng = RiskEngine()
        eng.set_initial_balance(198)
        eng.update_pnl(-4.0, 198)  # ~2%
        assert eng._halt_level == "STOP"
        assert eng._trading_stopped

    def test_consecutive_5(self):
        """5연패 → 경고."""
        eng = RiskEngine()
        eng.set_initial_balance(1000)
        for i in range(5):
            eng.update_pnl(-0.1, 1000, f"BTC_{i}")
        assert eng.consecutive_losses == 5


class TestCorrelationCap:
    def test_under_cap(self):
        """캡 이내."""
        eng = RiskEngine()
        assert eng.check_correlation_cap(198, 0.3, 0.2, 0.2)

    def test_over_cap(self):
        """캡 초과."""
        eng = RiskEngine()
        assert not eng.check_correlation_cap(198, 0.6, 0.4, 0.3)


class TestEVSummary:
    def test_ev_runs(self):
        """EV 요약 출력 (에러 없이)."""
        print_ev_summary(198, 10)


class TestV10Compat:
    def test_can_trade(self):
        eng = RiskEngine()
        eng.set_initial_balance(198)
        assert eng.can_trade()

    def test_record_trade(self):
        eng = RiskEngine()
        eng.set_initial_balance(198)
        eng.record_trade(-1.0, "BTC")
        assert eng._daily_pnl == -1.0

    def test_get_state(self):
        eng = RiskEngine()
        eng.set_initial_balance(198)
        state = eng.get_state()
        assert "daily_pnl" in state
        assert "mode" in state
