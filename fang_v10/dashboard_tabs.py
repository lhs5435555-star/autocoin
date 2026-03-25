"""대시보드 백그라운드 스레드: BacktestThread, OptimizerThread, BotThread."""
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
        pass


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
        pass


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
