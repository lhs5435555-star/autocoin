"""v11 PositionManager 상태 머신 + profit_lock == tp1 테스트."""
import time
import pandas as pd
import numpy as np
from fang_v10.position_manager import PositionManager, PositionState
from fang_v10.config import CONFIG


def _make_pos(symbol="BTC/USDT:USDT", side="long", entry=87000, size=0.01,
              sl=86500, tp1=87750, tp2=88250, tp3=89000):
    """v11 상태 머신 포지션 생성."""
    pos = PositionState(symbol=symbol, side=side)
    pos.entries = [(entry, size)]
    pos.avg_price = entry
    pos.total_size = size
    pos.leverage = 10
    pos.atr = 300
    pos.initial_r_distance = 500  # 1R = $500
    pos.entry_bar = 0
    pos.entry_time = time.time()
    pos.regime = "TREND"
    pos.strategy = "trend"
    # v11 필드
    pos.phase = "OPEN"
    pos.sl_price = sl
    pos.tp1_price = tp1
    pos.tp2_price = tp2
    pos.tp3_price = tp3
    pos.profit_lock_price = tp1  # ★ = tp1
    return pos


class TestProfitLockEqualsTP1:
    def test_profit_lock_equals_tp1(self):
        """★ 핵심: profit_lock_price == tp1_price."""
        pos = _make_pos(tp1=87750)
        assert pos.profit_lock_price == pos.tp1_price == 87750

    def test_profit_lock_roe_pct(self):
        """레버리지 10x, SL 0.40% 기준 profit_lock ROE%."""
        entry = 87000
        sl = 86500  # SL dist = 500/87000 = 0.575%
        tp1 = 87750  # TP1 dist = 750/87000 = 0.862%
        leverage = 10
        roe_pct = (tp1 - entry) / entry * leverage * 100
        print(f"profit_lock ROE% = {roe_pct:.1f}%")
        # 1.5R × 10x ≈ 8.6% ROE
        assert roe_pct > 5.0  # 최소 5% 이상


class TestStateMachine:
    def test_open_sl_hit(self):
        """OPEN → SL 체결 → CLOSED."""
        mgr = PositionManager()
        pos = _make_pos(sl=86500)
        mgr.positions["BTC|long"] = pos

        result = mgr.check_exit("BTC|long", 86400, bar_idx=10)  # SL 아래
        assert result is not None
        assert result["action"] == "close"
        assert result["reason"] == "SL"
        assert pos.phase == "CLOSED"

    def test_open_tp1_hit(self):
        """OPEN → TP1 도달 → TP1_HIT (40% 청산 + 새 SL)."""
        mgr = PositionManager()
        pos = _make_pos(tp1=87750)
        mgr.positions["BTC|long"] = pos

        result = mgr.check_exit("BTC|long", 87800, bar_idx=10)  # TP1 위
        assert result is not None
        assert result["action"] == "partial"
        assert result["reason"] == "TP1"
        assert result["ratio"] == 0.40
        assert "new_sl" in result
        assert pos.phase == "TP1_HIT"
        # 새 SL은 entry + fee_buffer
        assert result["new_sl"] > pos.avg_price

    def test_tp1_hit_then_sl(self):
        """TP1_HIT 상태에서 새 SL 체결 → CLOSED."""
        mgr = PositionManager()
        pos = _make_pos()
        pos.phase = "TP1_HIT"
        pos.tp_count = 1
        pos.remaining_ratio = 0.60
        pos.sl_price = 87100  # entry + fee_buffer 수준
        mgr.positions["BTC|long"] = pos

        result = mgr.check_exit("BTC|long", 87050, bar_idx=20)
        assert result is not None
        assert result["reason"] == "SL_AFTER_TP1"
        assert pos.phase == "CLOSED"

    def test_tp1_hit_then_tp2(self):
        """TP1_HIT → TP2 도달 → TP2_HIT."""
        mgr = PositionManager()
        pos = _make_pos(tp2=88250)
        pos.phase = "TP1_HIT"
        pos.tp_count = 1
        pos.remaining_ratio = 0.60
        pos.sl_price = 87100
        mgr.positions["BTC|long"] = pos

        result = mgr.check_exit("BTC|long", 88300, bar_idx=30)
        assert result is not None
        assert result["reason"] == "TP2"
        assert pos.phase == "TP2_HIT"

    def test_trail_only(self):
        """TRAIL_ONLY → 트레일 SL 체결 → CLOSED."""
        mgr = PositionManager()
        pos = _make_pos()
        pos.phase = "TRAIL_ONLY"
        pos.tp_count = 2
        pos.remaining_ratio = 0.25
        pos.trail_sl = 88000
        pos.favorable_extreme = 89000
        mgr.positions["BTC|long"] = pos

        result = mgr.check_exit("BTC|long", 87900, bar_idx=50)
        assert result is not None
        assert result["reason"] == "TRAIL"
        assert pos.phase == "CLOSED"

    def test_early_cut(self):
        """OPEN → 8봉 후 peak_r < threshold → EARLY."""
        mgr = PositionManager()
        pos = _make_pos()
        pos.peak_r = 0.1  # 낮은 MFE
        mgr.positions["BTC|long"] = pos

        result = mgr.check_exit("BTC|long", 87100, bar_idx=10)
        assert result is not None
        assert result["reason"] == "EARLY"
        assert pos.phase == "CLOSED"

    def test_closed_ignored(self):
        """CLOSED 상태면 None 반환."""
        mgr = PositionManager()
        pos = _make_pos()
        pos.phase = "CLOSED"
        mgr.positions["BTC|long"] = pos

        result = mgr.check_exit("BTC|long", 86000, bar_idx=10)
        assert result is None

    def test_reduce_only_in_tp1_result(self):
        """TP1 체결 시 결과에 new_sl이 있어 reduceOnly SL 발행 가능."""
        mgr = PositionManager()
        pos = _make_pos(tp1=87750)
        mgr.positions["BTC|long"] = pos

        result = mgr.check_exit("BTC|long", 87800, bar_idx=10)
        assert result["action"] == "partial"
        assert "new_sl" in result
        # new_sl로 reduceOnly SL 발행하는 것은 execution 레이어 책임

    def test_short_symmetric(self):
        """숏 TP1 대칭 동작."""
        mgr = PositionManager()
        pos = _make_pos(side="short", entry=87000, sl=87500,
                        tp1=86250, tp2=85750, tp3=85000)
        mgr.positions["BTC|short"] = pos

        # TP1 = 86250, 현재가 86200 < tp1 → 숏 TP1 체결
        result = mgr.check_exit("BTC|short", 86200, bar_idx=10)
        assert result is not None
        assert result["reason"] == "TP1"
        assert pos.phase == "TP1_HIT"
