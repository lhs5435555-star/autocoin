"""대시보드 백그라운드 스레드: BacktestThread, OptimizerThread, BotThread."""
import time

from PyQt5.QtCore import QThread, pyqtSignal


class BacktestThread(QThread):
    progress = pyqtSignal(int, str)
    finished = pyqtSignal(object)
    error = pyqtSignal(str)

    def __init__(self, symbol, months, balance):
        super().__init__()
        self.symbol = symbol
        self.months = months
        self.balance = balance
        self._cancel = False

    def stop(self):
        self._cancel = True

    def run(self):
        try:
            import pandas as pd
            from fang_v10.backtest import BacktestEngine
            from fang_v10.exchange_api import BitgetClient

            client = BitgetClient("", "", "", paper=True)

            # 1. since 기반 페이지네이션 (1000봉씩)
            days = self.months * 30
            now_ms = int(time.time() * 1000)
            start_ms = now_ms - days * 86400 * 1000
            since = start_ms
            frames = []

            while since < now_ms and not self._cancel:
                raw = client.exchange.fetch_ohlcv(
                    self.symbol, "5m", since=since, limit=1000,
                )
                if not raw:
                    break
                df = pd.DataFrame(
                    raw,
                    columns=["timestamp", "open", "high", "low", "close", "volume"],
                )
                frames.append(df)
                since = int(df.iloc[-1]["timestamp"]) + 1
                pct = min(20, int((since - start_ms) / (now_ms - start_ms) * 20))
                self.progress.emit(pct, f"데이터 {len(frames) * 1000}봉...")

            if self._cancel:
                return

            if not frames:
                self.error.emit("데이터 수집 실패")
                return

            full_df = (
                pd.concat(frames)
                .drop_duplicates(subset=["timestamp"])
                .sort_values("timestamp")
                .reset_index(drop=True)
            )
            self.progress.emit(25, f"데이터 완료 {len(full_df)}봉")

            # 2. 백테스트 실행
            engine = BacktestEngine(seed=42)
            result = engine.run(
                full_df, self.symbol, self.balance,
                progress_callback=lambda pct, msg: self.progress.emit(
                    25 + int(pct * 0.75), msg),
            )

            if not self._cancel:
                self.progress.emit(100, "완료")
                self.finished.emit(result)

        except Exception as e:
            self.error.emit(str(e))


class OptimizerThread(QThread):
    progress = pyqtSignal(int, str)
    finished = pyqtSignal(object)
    error = pyqtSignal(str)

    def __init__(self, df, symbol, balance):
        super().__init__()
        self.df = df
        self.symbol = symbol
        self.balance = balance
        self._cancel = False

    def stop(self):
        self._cancel = True

    def run(self):
        try:
            from fang_v10.optimizer import AutoOptimizer

            opt = AutoOptimizer(
                backtest_engine=None,
                df=self.df,
                symbol=self.symbol,
                initial_balance=self.balance,
            )
            result = opt.run_optimization(
                progress_callback=lambda p, m: self.progress.emit(p, m),
            )

            if not self._cancel:
                self.progress.emit(100, "최적화 완료")
                self.finished.emit(result)

        except Exception as e:
            self.error.emit(str(e))


class BotThread(QThread):
    """메인 트레이딩 루프 스레드 — main.py와 동일 수준."""

    state_updated = pyqtSignal(dict)
    trade_executed = pyqtSignal(dict)
    log_message = pyqtSignal(str)
    error_occurred = pyqtSignal(str)

    def __init__(self):
        super().__init__()
        self.running = False

    def stop(self):
        self.running = False

    def run(self):  # noqa: C901
        self.running = True
        from fang_v10.config import CONFIG
        from fang_v10.exchange_api import BitgetClient
        from fang_v10.execution import SafeExecutor
        from fang_v10.regime_engine import RegimeEngine, ensure_indicators
        from fang_v10.position_manager import PositionManager
        from fang_v10.risk_engine import RiskEngine, MddTracker
        from fang_v10.sizing_engine import calc_position_size
        from fang_v10.strategy import generate_signals
        from fang_v10.btc_filter import can_enter_eth
        from fang_v10.state_store import StateStore
        from fang_v10.monitor import (
            setup_logging, log_exit, log_state,
            log_phantom_stats, log_blocked_stats,
        )

        try:
            setup_logging()
            client = BitgetClient(
                CONFIG.API_KEY, CONFIG.API_SECRET,
                CONFIG.PASSPHRASE, CONFIG.PAPER_TRADING,
            )
            executor = SafeExecutor(client)
            regime_eng = RegimeEngine()
            pos_mgr = PositionManager()
            risk_eng = RiskEngine()
            mdd = MddTracker()
            store = StateStore()

            balance = client.fetch_balance() if not CONFIG.PAPER_TRADING else 198.0
            risk_eng.set_initial_balance(balance)

            # 재시작 복구
            saved_pos, saved_risk, saved_mdd = store.load()
            if not CONFIG.PAPER_TRADING:
                exchange_positions = []
                for s in CONFIG.SYMBOLS:
                    pos = client.get_position(s)
                    if pos:
                        exchange_positions.append(pos)
                saved_pos = store.reconcile(
                    saved_pos, exchange_positions, client, risk_eng,
                )

            loop_count = 0
            last_entry_candle = {s: None for s in CONFIG.SYMBOLS}
            self.log_message.emit("봇 시작 완료")

            while self.running:
                try:
                    btc_df = None
                    df = None

                    for symbol in CONFIG.SYMBOLS:
                        # ── 1. 캔들 갱신 ──
                        df = client.fetch_ohlcv(symbol, CONFIG.TIMEFRAME_PRIMARY, 300)
                        if df.empty:
                            continue
                        df = ensure_indicators(df)
                        if "BTC" in symbol:
                            btc_df = df

                        bar_idx = len(df) - 1
                        price = float(df.iloc[-1]["close"])
                        asset = "BTC" if "BTC" in symbol else "ETH"

                        # ── 2. 레짐 판별 ──
                        regime = regime_eng.detect(symbol, df, bar_idx)

                        # ── 3. Phantom/Blocked 업데이트 ──
                        pos_mgr.phantom.update(symbol, price, bar_idx)
                        pos_mgr.blocked.update(symbol, price, bar_idx)

                        # ── 4. 기존 포지션 관리 ──
                        ema9 = float(df.iloc[-1].get("ema9", price))
                        ema21 = float(df.iloc[-1].get("ema21", price))

                        for key, pos in list(pos_mgr.positions.items()):
                            if pos.symbol != symbol:
                                continue

                            # DCA 체크
                            atr = float(df.iloc[-1].get("atr", 0))
                            dca_result = pos_mgr.check_dca(
                                symbol, pos.side, price, atr, bar_idx,
                                ema9=ema9, ema21=ema21,
                            )
                            if dca_result:
                                sizing = calc_position_size(
                                    balance, price,
                                    pos.avg_price - pos.initial_r_distance,
                                    pos.avg_price + pos.initial_r_distance * CONFIG.TP1_R,
                                    symbol,
                                )
                                if sizing.valid:
                                    result = executor.safe_dca(symbol, pos.side, sizing)
                                    if result:
                                        pos.add_dca(price, sizing.amount)
                                        self.trade_executed.emit({
                                            "symbol": symbol, "type": "DCA",
                                            "price": price,
                                            "reason": f"DCA 새평균={pos.avg_price:.2f}",
                                        })

                            # 청산 체크
                            exit_sig = pos_mgr.check_exit(
                                key, price, bar_idx,
                                ema9=ema9, ema21=ema21, df=df,
                            )
                            if not exit_sig:
                                continue

                            action = exit_sig["action"]
                            reason = exit_sig["reason"]

                            if action == "close":
                                result = executor.safe_close(symbol, pos.side, reason)
                                if result:
                                    pnl = SafeExecutor.calc_paper_pnl(
                                        pos.side, pos.avg_price, price,
                                        pos.total_size, asset,
                                    )["net_pnl"] if CONFIG.PAPER_TRADING else 0
                                    r_val = pos.calc_risk_r(price)
                                    risk_eng.record_trade(pnl, symbol)
                                    executor.record_trade_result(
                                        symbol, pos.side, reason.lower(),
                                    )
                                    balance += pnl
                                    record = log_exit(
                                        symbol, pos.side, price, reason,
                                        f"{reason}: R={r_val:.2f}",
                                        pnl, r_val, pos.peak_r, pos.trough_r,
                                        bar_idx - pos.entry_bar, balance,
                                        regime=pos.regime, strategy=pos.strategy,
                                    )
                                    store.save_trade_log(record)
                                    del pos_mgr.positions[key]
                                    self.trade_executed.emit({
                                        "symbol": symbol, "type": reason,
                                        "price": price, "pnl": pnl,
                                        "r_value": r_val,
                                        "reason": f"{reason}: R={r_val:.2f}",
                                    })

                            elif action == "partial":
                                ratio = exit_sig["ratio"]
                                result = executor.safe_partial_close(
                                    symbol, pos.side, ratio, reason,
                                )
                                if result:
                                    pos.tp_count += 1
                                    pos.remaining_ratio -= ratio
                                    pos.total_size *= (1 - ratio)
                                    self.trade_executed.emit({
                                        "symbol": symbol, "type": reason,
                                        "price": price, "reason": reason,
                                    })

                            elif action == "move_sl":
                                new_sl = exit_sig.get("new_sl")
                                pos.be_activated = True
                                if new_sl and not CONFIG.PAPER_TRADING:
                                    try:
                                        client.cancel_trigger_orders(symbol)
                                        client.set_trigger_sl(
                                            symbol, pos.side, new_sl,
                                            pos.total_size,
                                        )
                                    except Exception as e:
                                        self.log_message.emit(f"BE SL 이동 실패: {e}")

                        # ── 5. 킬스위치 + MDD ──
                        if not risk_eng.can_trade():
                            continue
                        can_coin, _ = risk_eng.can_trade_coin(symbol)
                        if not can_coin:
                            continue
                        mdd_result = mdd.update(balance)
                        if not mdd_result["allowed"]:
                            continue
                        total_risk_r = sum(
                            1.0 for p in pos_mgr.positions.values()
                        )
                        if total_risk_r >= CONFIG.MAX_TOTAL_RISK_R:
                            continue
                        has_pos = any(
                            p.symbol == symbol for p in pos_mgr.positions.values()
                        )
                        if has_pos:
                            continue

                        # ── 6. BTC 필터 (ETH) ──
                        # ── 7. 신규 진입 ──
                        signals = generate_signals(
                            symbol, df, regime, last_entry_candle.get(symbol),
                        )
                        if not signals:
                            continue
                        sig = signals[0]

                        if "ETH" in symbol and btc_df is not None:
                            can_eth, eth_reason = can_enter_eth(btc_df, sig.side)
                            if not can_eth:
                                pos_mgr.blocked.register_blocked(
                                    symbol, sig.side, sig.entry_price,
                                    sig.sl_price, sig.tp_price,
                                    f"BTC필터:{eth_reason}", bar_idx,
                                )
                                continue

                        sizing = calc_position_size(
                            balance, sig.entry_price,
                            sig.sl_price, sig.tp_price, symbol,
                        )
                        if not sizing.valid:
                            pos_mgr.blocked.register_blocked(
                                symbol, sig.side, sig.entry_price,
                                sig.sl_price, sig.tp_price,
                                f"사이징:{sizing.reject_reason}", bar_idx,
                            )
                            continue

                        result = executor.safe_open(symbol, sig.side, sizing)
                        if result:
                            fill = result["fill_price"]
                            atr = float(df.iloc[-1].get("atr", 0))
                            pos_mgr.open_position(
                                symbol, sig.side, fill, sizing.amount,
                                sizing.leverage, atr, bar_idx,
                                regime.value, sig.strategy,
                            )
                            last_entry_candle[symbol] = bar_idx
                            self.trade_executed.emit({
                                "symbol": symbol, "type": "ENTRY",
                                "side": sig.side, "price": fill,
                                "reason": sig.reason,
                            })

                    # ── 8. 상태 저장 ──
                    store.save(
                        {k: vars(v) for k, v in pos_mgr.positions.items()},
                        risk_eng.get_risk_mode(),
                        mdd.update(balance),
                    )

                    loop_count += 1

                    # 60초마다 reconcile
                    if loop_count % 12 == 0 and not CONFIG.PAPER_TRADING:
                        exchange_positions = []
                        for sym in CONFIG.SYMBOLS:
                            pos = client.get_position(sym)
                            if pos:
                                exchange_positions.append(pos)
                        local_pos = {k: vars(v) for k, v in pos_mgr.positions.items()}
                        store.reconcile(
                            local_pos, exchange_positions, client, risk_eng,
                        )

                    # 5분마다 상태 로그
                    if loop_count % 60 == 0:
                        regimes = {}
                        for sym in CONFIG.SYMBOLS:
                            if df is not None and not df.empty:
                                regimes[sym] = regime_eng.detect(
                                    sym, df, len(df) - 1,
                                ).value
                        log_state(
                            balance, pos_mgr.positions, 0,
                            regimes, risk_eng.get_risk_mode(),
                        )
                        log_phantom_stats(pos_mgr.phantom.get_stats())
                        log_blocked_stats(pos_mgr.blocked.get_stats())

                    # 상태 emit
                    risk_mode = risk_eng.get_risk_mode()
                    self.state_updated.emit({
                        "balance": balance,
                        "daily_pnl": getattr(risk_eng, "_daily_pnl", 0),
                        "risk_mode": risk_mode.get("mode", "NORMAL"),
                        "size_mult": risk_mode.get("size_mult", 1.0),
                        "positions": {
                            k: {
                                "symbol": v.symbol, "side": v.side,
                                "avg_price": v.avg_price,
                                "regime": v.regime, "dca_count": v.dca_count,
                                "tp_count": v.tp_count,
                                "be_activated": v.be_activated,
                            }
                            for k, v in pos_mgr.positions.items()
                        },
                    })

                    time.sleep(CONFIG.MAIN_LOOP_SEC)
                except Exception as e:
                    self.error_occurred.emit(str(e))
                    time.sleep(30)

            # 종료 시 상태 저장
            store.save(
                {k: vars(v) for k, v in pos_mgr.positions.items()},
                risk_eng.get_risk_mode(),
                mdd.update(balance),
            )
            self.log_message.emit("봇 정지 완료")

        except Exception as e:
            self.error_occurred.emit(f"봇 초기화 실패: {e}")
