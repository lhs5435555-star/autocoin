"""
백테스트 엔진.

라이브 전략/사이징/리스크 모듈을 그대로 import하여 판단 로직 공유.
체결 시뮬레이션은 백테스트 전용.
"""

from __future__ import annotations

import logging
import random
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import pandas as pd

from fang_v10.config import CONFIG
from fang_v10.position_manager import (
    BlockedSignalTracker,
    PhantomTracker,
    PositionManager,
    PositionState,
)
from fang_v10.regime_engine import MarketRegime, RegimeEngine, ensure_indicators
from fang_v10.risk_engine import RiskEngine
from fang_v10.sizing_engine import SizingResult, calc_position_size
from fang_v10.strategy import Signal, generate_signals

logger = logging.getLogger(__name__)


@dataclass
class BacktestResult:
    """백테스트 결과."""
    total_trades: int = 0
    wins: int = 0
    losses: int = 0
    winrate: float = 0.0
    total_pnl: float = 0.0
    profit_factor: float = 0.0
    max_drawdown: float = 0.0          # equity peak-to-trough
    avg_r: float = 0.0                  # 실측 평균 R
    equity_curve: List[float] = field(default_factory=list)  # 복리
    regime_stats: Dict = field(default_factory=dict)
    tp_stats: Dict = field(default_factory=dict)
    dca_stats: Dict = field(default_factory=dict)
    sl_phantom_stats: Dict = field(default_factory=dict)
    sl_phantom_recommendation: str = ""
    blocked_signal_stats: Dict = field(default_factory=dict)
    total_fee_cost: float = 0.0
    total_slippage_cost: float = 0.0
    be_exit_rate: float = 0.0           # BE 활성화 후 BE에서 나간 비율
    tp_extension_rate: float = 0.0      # TP1→TP2 연장률
    same_bar_conflict_rate: float = 0.0  # same-bar SL+TP 동시 비율

    # 진단 카운터
    diag_regime_bars: Dict = field(default_factory=dict)
    diag_signals_generated: int = 0
    diag_blocked_has_pos: int = 0
    diag_blocked_cooldown: int = 0
    diag_blocked_sizing: int = 0
    diag_blocked_killswitch: int = 0


class BacktestEngine:
    """백테스트 엔진."""

    def __init__(self, slippage_pct: float = 0.04, seed: int = 42) -> None:
        self.slippage_pct = slippage_pct
        self.rng = random.Random(seed)
        self.regime_eng = RegimeEngine()
        self.risk_eng = RiskEngine()

    def run(
        self,
        df: pd.DataFrame,
        symbol: str,
        initial_balance: float = 198.0,
        btc_df: Optional[pd.DataFrame] = None,
        progress_callback=None,
    ) -> BacktestResult:
        """백테스트 실행.

        Args:
            df: OHLCV DataFrame (최소 50봉)
            symbol: 심볼
            initial_balance: 초기 잔고 (USDT)
            btc_df: BTC OHLCV (ETH BTC필터용, 없으면 무시)

        Returns:
            BacktestResult
        """
        # Edge case: 빈 df 또는 데이터 부족
        required_cols = {"open", "high", "low", "close", "volume"}
        if df is None or df.empty or len(df) < 50:
            return BacktestResult()
        if not required_cols.issubset(set(df.columns)):
            return BacktestResult()

        df = ensure_indicators(df)
        asset = "BTC" if "BTC" in symbol else "ETH"

        # 1H 리샘플 + 지표 (레짐용)
        from fang_v10.regime_engine import resample_to_1h, compute_1h_indicators
        df_1h_full = resample_to_1h(df)
        if not df_1h_full.empty:
            df_1h_full = compute_1h_indicators(df_1h_full)

        pos_mgr = PositionManager()
        # 백테스트 모드 (HALT_NEW_ENTRIES 우회 + 킬스위치 비활성)
        CONFIG._BACKTEST_MODE = True
        self.risk_eng = RiskEngine()
        self.risk_eng.set_initial_balance(initial_balance)

        equity = initial_balance
        peak_equity = initial_balance
        result = BacktestResult()
        result.equity_curve.append(equity)

        # 상태 추적
        all_r_results: List[float] = []
        gross_wins = 0.0
        gross_losses = 0.0
        total_fee = 0.0
        total_slip = 0.0
        same_bar_conflicts = 0
        be_activated_count = 0
        be_exit_count = 0
        tp1_count = 0
        tp2_count = 0

        # 쿨다운 (봉 인덱스 기준)
        cooldown_until: Dict[str, int] = {}
        sl_dir_cooldown: Dict[str, int] = {}

        # Pending
        pending_signal: Optional[Signal] = None
        pending_sizing: Optional[SizingResult] = None
        pending_dca: Dict[str, Dict] = {}

        last_entry_candle: Optional[int] = None

        # 레짐/TP 통계
        regime_stats: Dict[str, Dict] = {}
        tp_stats: Dict[str, int] = {"TP1": 0, "TP2": 0, "TRAIL": 0, "SL": 0,
                                      "BE": 0, "EARLY": 0, "TIME": 0,
                                      "TREND_REV": 0, "EMERGENCY": 0}
        dca_stats = {"attempted": 0, "executed": 0}

        total_bars = len(df) - 50

        for i in range(50, len(df)):
            if progress_callback and (i - 50) % 500 == 0 and total_bars > 0:
                pct = (i - 50) / total_bars * 100
                progress_callback(pct, "백테스트 진행중...")

            row = df.iloc[i]
            bar_open = row["open"]
            bar_high = row["high"]
            bar_low = row["low"]
            bar_close = row["close"]

            # ── 1. Pending 체결 (i-1봉 신호 → i봉 open) ──
            if pending_signal and pending_sizing:
                entry_price = bar_open
                sl_price = pending_sizing.sl_price

                # Gap 방지: open이 이미 SL을 넘었으면 진입 취소
                skip = False
                if pending_signal.side == "long" and entry_price <= sl_price:
                    skip = True
                elif pending_signal.side == "short" and entry_price >= sl_price:
                    skip = True

                if not skip:
                    slip = self._apply_slippage(entry_price, pending_signal.side, "entry")
                    total_slip += abs(slip - entry_price) * pending_sizing.amount

                    # 수수료
                    fee = slip * pending_sizing.amount * CONFIG.EFFECTIVE_TAKER_FEE
                    total_fee += fee

                    atr = df.iloc[i].get("atr", 0)
                    pos_mgr.open_position(
                        symbol=symbol, side=pending_signal.side,
                        entry_price=slip, size=pending_sizing.amount,
                        leverage=pending_sizing.leverage, atr=atr,
                        bar_idx=i, regime=pending_signal.regime.value,
                        strategy=pending_signal.strategy,
                    )
                    last_entry_candle = i

                pending_signal = None
                pending_sizing = None

            # ── Pending DCA 체결 ──
            if symbol in pending_dca:
                dca_info = pending_dca.pop(symbol)
                key = dca_info["key"]
                pos = pos_mgr.positions.get(key)
                if pos:
                    dca_price = self._apply_slippage(bar_open, pos.side, "entry")
                    dca_size = pos.total_size * CONFIG.DCA_SIZE_RATIO
                    fee = dca_price * dca_size * CONFIG.EFFECTIVE_TAKER_FEE
                    total_fee += fee
                    pos.add_dca(dca_price, dca_size)
                    dca_stats["executed"] += 1

            # ── 2. 레짐 판별 (1H 완성봉 기반) ──
            # 15m bar → 1H bar 매핑 (4봉 = 1H 1봉)
            if not df_1h_full.empty:
                bar_1h_idx = min(i // 4, len(df_1h_full) - 1)
                df_1h_slice = df_1h_full.iloc[:bar_1h_idx + 1]
                if len(df_1h_slice) >= 50:
                    from fang_v10.regime_engine import detect_regime
                    regime_result = detect_regime(symbol, df_1h_slice)
                    regime = regime_result.v10_regime
                else:
                    regime = MarketRegime.BOX
            else:
                regime = self.regime_eng.detect(symbol, df, i)
            regime_key = regime.value
            if regime_key not in regime_stats:
                regime_stats[regime_key] = {"bars": 0, "trades": 0, "pnl": 0.0}
            regime_stats[regime_key]["bars"] += 1

            # ── 3. Phantom/Blocked 업데이트 ──
            pos_mgr.phantom.update(symbol, bar_close, i)
            pos_mgr.blocked.update(symbol, bar_close, i)

            # ── 4. 기존 포지션 관리 ──
            keys_to_close: List[Tuple[str, Dict]] = []
            for key, pos in list(pos_mgr.positions.items()):
                if pos.symbol != symbol:
                    continue

                # SL/TP 도달 체크 (봉 내 high/low)
                ema9 = df.iloc[i].get("ema9")
                ema21 = df.iloc[i].get("ema21")

                # 같은 봉 SL+TP 동시 → SL 우선
                sl_hit = False
                tp_hit = False

                current_r_low = pos.calc_risk_r(bar_low)
                current_r_high = pos.calc_risk_r(bar_high)

                if pos.side == "long":
                    if current_r_low <= -1.0:
                        sl_hit = True
                    if current_r_high >= CONFIG.TP1_R and pos.tp_count == 0:
                        tp_hit = True
                else:
                    if current_r_high <= -1.0:
                        sl_hit = True
                    if current_r_low >= CONFIG.TP1_R and pos.tp_count == 0:
                        tp_hit = True

                if sl_hit and tp_hit:
                    same_bar_conflicts += 1

                # check_exit with close price for non-intrabar checks
                exit_result = pos_mgr.check_exit(
                    key, bar_close, i, ema9=ema9, ema21=ema21,
                )

                # Intrabar SL override
                if sl_hit:
                    exit_result = {"action": "close", "ratio": 1.0, "reason": "SL"}

                if exit_result:
                    action = exit_result["action"]
                    reason = exit_result["reason"]

                    if action == "close":
                        # 청산 가격 결정
                        if reason == "SL":
                            if pos.side == "long":
                                exit_price = pos.avg_price - pos.initial_r_distance
                            else:
                                exit_price = pos.avg_price + pos.initial_r_distance
                            exit_price = max(bar_low, min(bar_high, exit_price))
                        elif reason in ("TRAIL", "EMERGENCY"):
                            exit_price = bar_close
                        else:
                            exit_price = bar_close

                        exit_fill = self._apply_slippage(exit_price, pos.side, "exit")
                        fee = exit_fill * pos.total_size * CONFIG.EFFECTIVE_TAKER_FEE
                        total_fee += fee
                        total_slip += abs(exit_fill - exit_price) * pos.total_size

                        # PnL 계산
                        if pos.side == "long":
                            pnl = (exit_fill - pos.avg_price) * pos.total_size - fee
                        else:
                            pnl = (pos.avg_price - exit_fill) * pos.total_size - fee

                        r_val = pos.calc_risk_r(exit_fill)
                        all_r_results.append(r_val)

                        if pnl >= 0:
                            result.wins += 1
                            gross_wins += pnl
                        else:
                            result.losses += 1
                            gross_losses += abs(pnl)

                        equity += pnl
                        result.total_pnl += pnl

                        # 통계 (진입 시점 레짐 기준)
                        tp_stats[reason] = tp_stats.get(reason, 0) + 1
                        entry_regime = pos.regime or regime_key
                        if entry_regime not in regime_stats:
                            regime_stats[entry_regime] = {"bars": 0, "trades": 0, "pnl": 0.0}
                        regime_stats[entry_regime]["trades"] += 1
                        regime_stats[entry_regime]["pnl"] += pnl

                        if pos.be_activated and reason == "BE":
                            be_exit_count += 1

                        # SL 유령 등록은 check_exit에서 처리됨

                        # 쿨다운 설정
                        if reason == "SL":
                            cd_bars = CONFIG.COOLDOWN_AFTER_SL_SEC // 300
                            cooldown_until[symbol] = i + cd_bars
                            sl_dir_cooldown[f"{symbol}|{pos.side}"] = (
                                i + CONFIG.COOLDOWN_AFTER_SL_SAME_DIR_SEC // 300
                            )
                            self.risk_eng.record_trade(-abs(pnl), symbol)
                        else:
                            cooldown_until[symbol] = i + CONFIG.COOLDOWN_NORMAL_SEC // 300
                            self.risk_eng.record_trade(pnl, symbol)

                        keys_to_close.append((key, exit_result))
                        result.total_trades += 1

                    elif action == "partial":
                        ratio = exit_result["ratio"]
                        close_size = pos.total_size * ratio

                        exit_fill = self._apply_slippage(bar_close, pos.side, "exit")
                        fee = exit_fill * close_size * CONFIG.EFFECTIVE_TAKER_FEE
                        total_fee += fee

                        if pos.side == "long":
                            pnl = (exit_fill - pos.avg_price) * close_size - fee
                        else:
                            pnl = (pos.avg_price - exit_fill) * close_size - fee

                        equity += pnl
                        result.total_pnl += pnl
                        pos.realized_pnl += pnl
                        pos.total_size -= close_size
                        pos.remaining_ratio -= ratio
                        pos.tp_count += 1

                        tp_stats[reason] = tp_stats.get(reason, 0) + 1

                        if reason == "TP1":
                            tp1_count += 1
                        elif reason == "TP2":
                            tp2_count += 1

                    elif action == "move_sl":
                        pos.be_activated = True
                        be_activated_count += 1

                # Peak 업데이트
                pos.update_peaks(bar_high)
                pos.update_peaks(bar_low)

            # 청산된 포지션 제거
            for key, _ in keys_to_close:
                pos_mgr.positions.pop(key, None)

            # ── 5. DCA 체크 ──
            for key, pos in pos_mgr.positions.items():
                if pos.symbol != symbol:
                    continue
                atr = df.iloc[i].get("atr", 0)
                ema9 = df.iloc[i].get("ema9")
                ema21 = df.iloc[i].get("ema21")
                dca_result = pos_mgr.check_dca(
                    symbol, pos.side, bar_close, atr, i,
                    ema9=ema9, ema21=ema21,
                )
                if dca_result:
                    dca_stats["attempted"] += 1
                    pending_dca[symbol] = {"key": key, **dca_result}

            # ── 6. 신규 진입 신호 ──
            # 레짐 카운트
            result.diag_regime_bars[regime.value] = result.diag_regime_bars.get(regime.value, 0) + 1

            # 백테스트에서는 킬스위치 스킵 (시뮬레이션 시간 리셋 불가)
            # 라이브에서만 적용

            # 이미 포지션 있으면 스킵
            has_pos = any(p.symbol == symbol for p in pos_mgr.positions.values())
            if has_pos:
                result.diag_blocked_has_pos += 1
                continue

            # 쿨다운 체크
            if i < cooldown_until.get(symbol, 0):
                # 차단 시 신호 생성 후 blocked 등록
                signals = generate_signals(symbol, df, regime, last_entry_candle, bar_idx=i)
                for sig in signals:
                    dir_key = f"{symbol}|{sig.side}"
                    if i < sl_dir_cooldown.get(dir_key, 0):
                        reason = "sl_same_dir_cooldown"
                    else:
                        reason = "cooldown"
                    pos_mgr.blocked.register_blocked(
                        symbol, sig.side, sig.entry_price,
                        sig.sl_price, sig.tp_price, reason, i,
                    )
                continue

            # 신호 생성
            signals = generate_signals(symbol, df, regime, last_entry_candle, bar_idx=i)
            if not signals:
                continue

            result.diag_signals_generated += 1
            sig = signals[0]

            # 방향별 쿨다운
            dir_key = f"{symbol}|{sig.side}"
            if i < sl_dir_cooldown.get(dir_key, 0):
                pos_mgr.blocked.register_blocked(
                    symbol, sig.side, sig.entry_price,
                    sig.sl_price, sig.tp_price, "sl_same_dir_cooldown", i,
                )
                continue

            # 사이징 (복리: 현재 equity 기준)
            sizing = calc_position_size(
                balance=equity, entry=sig.entry_price,
                sl=sig.sl_price, tp=sig.tp_price, symbol=symbol,
            )
            if not sizing.valid:
                result.diag_blocked_sizing += 1
                pos_mgr.blocked.register_blocked(
                    symbol, sig.side, sig.entry_price,
                    sig.sl_price, sig.tp_price,
                    f"sizing_reject:{sizing.reject_reason}", i,
                )
                continue

            # Pending (다음 봉 open으로 체결)
            pending_signal = sig
            pending_sizing = sizing

            # Equity curve + drawdown
            result.equity_curve.append(equity)
            if equity > peak_equity:
                peak_equity = equity
            dd = (peak_equity - equity) / peak_equity if peak_equity > 0 else 0
            if dd > result.max_drawdown:
                result.max_drawdown = dd

        # ── 최종 통계 ──
        result.equity_curve.append(equity)

        # Max drawdown (equity curve 전체 재계산)
        peak = result.equity_curve[0]
        for eq in result.equity_curve:
            if eq > peak:
                peak = eq
            dd = (peak - eq) / peak if peak > 0 else 0
            if dd > result.max_drawdown:
                result.max_drawdown = dd

        result.winrate = result.wins / result.total_trades if result.total_trades > 0 else 0
        result.avg_r = sum(all_r_results) / len(all_r_results) if all_r_results else 0
        result.profit_factor = gross_wins / gross_losses if gross_losses > 0 else 0.0
        result.regime_stats = regime_stats
        result.tp_stats = tp_stats
        result.dca_stats = dca_stats
        result.total_fee_cost = total_fee
        result.total_slippage_cost = total_slip

        result.sl_phantom_stats = pos_mgr.phantom.get_stats()
        result.sl_phantom_recommendation = pos_mgr.phantom.get_recommendation()
        result.blocked_signal_stats = pos_mgr.blocked.get_stats()

        result.be_exit_rate = (
            be_exit_count / be_activated_count if be_activated_count > 0 else 0
        )
        result.tp_extension_rate = tp2_count / tp1_count if tp1_count > 0 else 0
        result.same_bar_conflict_rate = (
            same_bar_conflicts / result.total_trades if result.total_trades > 0 else 0
        )

        self._print_summary(result)
        return result

    def _apply_slippage(
        self, price: float, side: str, direction: str,
    ) -> float:
        """슬리피지 적용.

        Args:
            price: 기준 가격
            side: "long" / "short"
            direction: "entry" / "exit"

        Returns:
            슬리피지 적용된 가격
        """
        slip = self.rng.uniform(0, self.slippage_pct / 100)

        # 불리한 방향으로 슬리피지
        if (side == "long" and direction == "entry") or \
           (side == "short" and direction == "exit"):
            return price * (1 + slip)
        return price * (1 - slip)

    @staticmethod
    def _print_summary(r: BacktestResult) -> None:
        """결과 요약 출력."""
        logger.info("=" * 60)
        logger.info("백테스트 결과")
        logger.info("=" * 60)
        logger.info("총 거래: %d (승 %d / 패 %d)", r.total_trades, r.wins, r.losses)
        logger.info("승률: %.1f%%", r.winrate * 100)
        logger.info("총 PnL: %.2f USDT", r.total_pnl)
        logger.info("Profit Factor: %.2f", r.profit_factor)
        logger.info("최대 DD: %.2f%%", r.max_drawdown * 100)
        logger.info("실측 평균 R: %.3f", r.avg_r)
        logger.info("총 수수료: %.2f, 총 슬리피지: %.2f", r.total_fee_cost, r.total_slippage_cost)
        logger.info("BE 이탈률: %.1f%%, TP 연장률: %.1f%%",
                     r.be_exit_rate * 100, r.tp_extension_rate * 100)
        logger.info("Same-bar 충돌률: %.1f%%", r.same_bar_conflict_rate * 100)
        logger.info("TP 통계: %s", r.tp_stats)
        logger.info("레짐 통계: %s", r.regime_stats)
        logger.info("DCA 통계: %s", r.dca_stats)
        logger.info("SL Phantom: %s → %s", r.sl_phantom_stats, r.sl_phantom_recommendation)
        logger.info("Blocked: %s", r.blocked_signal_stats)
        logger.info("=" * 60)
