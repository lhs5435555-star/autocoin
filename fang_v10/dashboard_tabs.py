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
            result = engine.run(full_df, self.symbol, self.balance)

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

            opt = AutoOptimizer()
            result = opt.run_optimization(
                self.df, self.symbol, self.balance,
            )

            if not self._cancel:
                self.progress.emit(100, "최적화 완료")
                self.finished.emit(result)

        except Exception as e:
            self.error.emit(str(e))


class BotThread(QThread):
    state_updated = pyqtSignal(dict)
    trade_executed = pyqtSignal(dict)
    error_occurred = pyqtSignal(str)

    def __init__(self):
        super().__init__()
        self.running = False

    def stop(self):
        self.running = False

    def run(self):
        self.running = True
        from fang_v10.config import CONFIG
        from fang_v10.exchange_api import BitgetClient
        from fang_v10.regime_engine import RegimeEngine, ensure_indicators
        from fang_v10.position_manager import PositionManager
        from fang_v10.risk_engine import RiskEngine
        from fang_v10.strategy import generate_signals

        try:
            client = BitgetClient(
                CONFIG.API_KEY, CONFIG.API_SECRET,
                CONFIG.PASSPHRASE, CONFIG.PAPER_TRADING,
            )
            regime_eng = RegimeEngine()
            pos_mgr = PositionManager()
            risk_eng = RiskEngine()

            balance = client.fetch_balance() if not CONFIG.PAPER_TRADING else 198.0
            risk_eng.set_initial_balance(balance)

            while self.running:
                try:
                    btc_df = None
                    for symbol in CONFIG.SYMBOLS:
                        df = client.fetch_ohlcv(symbol, CONFIG.TIMEFRAME_PRIMARY, 300)
                        if df.empty:
                            continue
                        df = ensure_indicators(df)
                        if "BTC" in symbol:
                            btc_df = df

                        bar_idx = len(df) - 1
                        price = float(df.iloc[-1]["close"])
                        regime = regime_eng.detect(symbol, df, bar_idx)

                        # 포지션 관리
                        ema9 = float(df.iloc[-1].get("ema9", price))
                        ema21 = float(df.iloc[-1].get("ema21", price))
                        for key in list(pos_mgr.positions.keys()):
                            if symbol in key:
                                exit_sig = pos_mgr.check_exit(
                                    key, price, bar_idx,
                                    ema9=ema9, ema21=ema21, df=df,
                                )
                                if exit_sig:
                                    r_val = 0
                                    if key in pos_mgr.positions:
                                        r_val = pos_mgr.positions[key].calc_risk_r(price)
                                    self.trade_executed.emit({
                                        "symbol": symbol,
                                        "type": exit_sig.get("reason", ""),
                                        "price": price,
                                        "r_value": r_val,
                                    })

                        # 신호 생성
                        if risk_eng.can_trade():
                            can_trade_coin, reason = risk_eng.can_trade_coin(symbol)
                            if can_trade_coin:
                                signals = generate_signals(symbol, df, regime, -1)
                                if signals:
                                    self.trade_executed.emit({
                                        "symbol": symbol,
                                        "type": "SIGNAL",
                                        "side": signals[0].side,
                                        "reason": signals[0].reason,
                                    })

                    # 상태 업데이트
                    risk_mode = risk_eng.get_risk_mode()
                    self.state_updated.emit({
                        "balance": balance,
                        "daily_pnl": getattr(risk_eng, "_daily_pnl", 0),
                        "risk_mode": risk_mode.get("mode", "NORMAL"),
                        "size_mult": risk_mode.get("size_mult", 1.0),
                        "positions": len(pos_mgr.positions),
                    })

                    time.sleep(CONFIG.MAIN_LOOP_SEC)
                except Exception as e:
                    self.error_occurred.emit(str(e))
                    time.sleep(30)
        except Exception as e:
            self.error_occurred.emit(f"봇 초기화 실패: {e}")
