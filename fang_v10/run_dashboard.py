"""PyQt5 대시보드 런처."""
import sys
from PyQt5.QtWidgets import QApplication
from fang_v10.dashboard import Dashboard

app = QApplication(sys.argv)
window = Dashboard()
window.show()
sys.exit(app.exec_())
