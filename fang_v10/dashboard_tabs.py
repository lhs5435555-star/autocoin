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
        pass
