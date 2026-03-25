"""
PyQt5 데스크톱 대시보드 — FANG SCALPER v10.

탭 6개: 현황 / 거래내역 / 분석 / 백테스트 / 설정 / 로그.
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import time as _time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Dict, List

from PyQt5.QtCore import Qt, QTimer, pyqtSignal
from PyQt5.QtGui import QColor, QFont, QTextCursor
from PyQt5.QtWidgets import (
    QApplication, QComboBox, QDoubleSpinBox, QFrame, QGridLayout,
    QGroupBox, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMainWindow,
    QMessageBox, QProgressBar, QPushButton, QScrollArea, QSpinBox,
    QTabWidget, QTableWidget, QTableWidgetItem, QTextEdit, QVBoxLayout,
    QWidget,
)

from matplotlib.backends.backend_qt5agg import FigureCanvasQTAgg
from matplotlib.figure import Figure

from fang_v10.config import CONFIG

DATA_DIR = Path(CONFIG.DATA_DIR)
TRADES_FILE = DATA_DIR / "trades_log.jsonl"
STATE_FILE = DATA_DIR / "state.json"

# ── 색상 ──
C_BG = "#1a1a2e"
C_CARD = "#16213e"
C_TABLE = "#0f3460"
C_HEADER = "#533483"
C_TEXT = "#e0e0e0"
C_GREEN = "#00e676"
C_RED = "#ff5252"
C_ACCENT = "#7c4dff"
C_YELLOW = "#ffd740"
C_BLUE = "#448aff"
C_ORANGE = "#ff9100"
C_GRAY = "#666666"
C_ROW_ALT = "#1a1a3e"

DARK_STYLE = f"""
QMainWindow, QWidget {{ background: {C_BG}; color: {C_TEXT}; font-size: 13pt; }}
QTabWidget::pane {{ border: 1px solid {C_HEADER}; }}
QTabBar::tab {{ background: {C_CARD}; color: {C_TEXT}; padding: 10px 24px;
               border-top-left-radius: 6px; border-top-right-radius: 6px; margin-right: 2px; }}
QTabBar::tab:selected {{ background: {C_HEADER}; font-weight: bold; }}
QTableWidget {{ background: {C_TABLE}; alternate-background-color: {C_ROW_ALT};
               gridline-color: #2a2a4e; font-size: 12pt; color: {C_TEXT}; border: none; }}
QHeaderView::section {{ background: {C_HEADER}; color: {C_TEXT}; padding: 6px;
                        border: none; font-weight: bold; font-size: 12pt; }}
QComboBox {{ background: {C_CARD}; color: {C_TEXT}; border: 1px solid {C_HEADER};
            border-radius: 4px; padding: 4px 8px; min-width: 90px; }}
QComboBox QAbstractItemView {{ background: {C_CARD}; color: {C_TEXT}; selection-background-color: {C_HEADER}; }}
QGroupBox {{ background: {C_CARD}; border: 1px solid {C_HEADER}; border-radius: 8px;
            margin-top: 12px; padding-top: 20px; font-weight: bold; }}
QGroupBox::title {{ subcontrol-origin: margin; left: 12px; color: {C_ACCENT}; }}
QScrollBar:vertical {{ background: {C_BG}; width: 8px; }}
QScrollBar::handle:vertical {{ background: {C_HEADER}; border-radius: 4px; }}
"""


# ════════════════════════════════════════════
# 유틸
# ════════════════════════════════════════════

def _load_trades() -> List[Dict]:
    if not TRADES_FILE.exists():
        return []
    trades = []
    for line in TRADES_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            try:
                trades.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return trades


def _load_state() -> Dict:
    if not STATE_FILE.exists():
        return {}
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def _pnl_color(val: float) -> QColor:
    return QColor(C_GREEN) if val >= 0 else QColor(C_RED)


def _make_item(text: str, align: int = Qt.AlignCenter, fg: QColor = None) -> QTableWidgetItem:
    item = QTableWidgetItem(str(text))
    item.setTextAlignment(align)
    item.setFlags(item.flags() & ~Qt.ItemIsEditable)
    if fg:
        item.setForeground(fg)
    return item


def _card(title: str, value: str, color: str = C_TEXT) -> QFrame:
    frame = QFrame()
    frame.setStyleSheet(f"background:{C_CARD}; border-radius:8px; padding:12px;")
    frame.setMinimumHeight(90)
    lay = QVBoxLayout(frame)
    lay.setContentsMargins(16, 10, 16, 10)
    lbl_title = QLabel(title)
    lbl_title.setStyleSheet(f"color:#999; font-size:11pt;")
    lbl_val = QLabel(value)
    lbl_val.setObjectName("card_val")
    lbl_val.setStyleSheet(f"color:{color}; font-size:18pt; font-weight:bold;")
    lay.addWidget(lbl_title)
    lay.addWidget(lbl_val)
    return frame


TYPE_COLORS = {
    "TP1": C_GREEN, "TP2": C_GREEN, "TRAIL": C_YELLOW,
    "SL": C_RED, "EMERGENCY": C_RED,
    "BE": C_YELLOW, "DCA": C_BLUE,
    "EARLY": C_ORANGE, "TIME": C_ORANGE, "TREND_REV": C_ORANGE,
    "ENTRY": C_ACCENT,
}


# ════════════════════════════════════════════
# Dashboard
# ════════════════════════════════════════════

class _QTextEditHandler(logging.Handler):
    """Python logging → pyqtSignal 브릿지."""

    def __init__(self, signal):
        super().__init__()
        self.signal = signal

    def emit(self, record):
        try:
            msg = self.format(record)
            self.signal.emit(msg)
        except Exception:
            pass


class Dashboard(QMainWindow):
    log_signal = pyqtSignal(str)

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("FANG SCALPER v10")
        self.setMinimumSize(1400, 900)

        self.bot = None
        self._bt_thread = None
        self._bt_last_result = None
        self._last_bt_df = None
        self._last_bt_symbol = None
        self._bot_start_time = 0

        tabs = QTabWidget()
        self.setCentralWidget(tabs)
        tabs.addTab(self._build_tab1(), "📊 현황")
        tabs.addTab(self._build_tab2(), "📋 거래내역")
        tabs.addTab(self._build_tab3(), "📈 분석")
        tabs.addTab(self._build_tab4(), "🔬 백테스트")
        tabs.addTab(self._build_tab5(), "⚙ 설정")
        tabs.addTab(self._build_tab6(), "\U0001f4dc 로그")

        # logging → 탭6 연결
        self._log_lines: List[str] = []
        self.log_signal.connect(self._append_log_line)
        self._setup_log_handler()

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._refresh)
        self._timer.start(5000)
        self._refresh()

    # ──────────── 탭 1: 현황 ────────────
    def _build_tab1(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)

        # 카드 행
        cards = QHBoxLayout()
        self.c_bal = _card("💰 운용 잔고", "$—")
        self.c_pnl = _card("📈 오늘 PnL", "$—")
        self.c_cum = _card("📊 누적 수익률", "—%")
        self.c_risk = _card("🛡 리스크", "—")
        # 잔고 카드에 부가 설명
        bal_note = QLabel("(실제 거래소 잔고와 다를 수 있음)")
        bal_note.setStyleSheet("color:#666; font-size:9pt; margin:0; padding:0;")
        self.c_bal.layout().addWidget(bal_note)
        for c in (self.c_bal, self.c_pnl, self.c_cum, self.c_risk):
            cards.addWidget(c)
        lay.addLayout(cards)

        # 포지션 테이블
        self.pos_table = QTableWidget(0, 9)
        self.pos_table.setHorizontalHeaderLabels(
            ["심볼", "방향", "레짐", "전략", "평균가", "현재R", "PnL($)", "DCA", "상태"])
        self.pos_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.pos_table.setAlternatingRowColors(True)
        self.pos_table.verticalHeader().setVisible(False)
        self.pos_table.setMinimumHeight(200)
        lay.addWidget(self.pos_table, stretch=1)

        # 하단 상태바
        bot = QHBoxLayout()
        self.lbl_regime = QLabel("BTC: — | ETH: —")
        self.lbl_regime.setTextFormat(Qt.RichText)
        self.lbl_regime.setStyleSheet(f"color:{C_ACCENT}; font-size:12pt;")
        self.lbl_kill = QLabel("일간: — | 연속패: — | MDD: —")
        self.lbl_kill.setStyleSheet(f"color:#999; font-size:12pt;")
        bot.addWidget(self.lbl_regime)
        bot.addStretch()
        bot.addWidget(self.lbl_kill)
        lay.addLayout(bot)
        return w

    # ──────────── 탭 2: 거래내역 ────────────
    def _build_tab2(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)

        # 필터 바
        filt = QHBoxLayout()
        self.cmb_sym = QComboBox()
        self.cmb_sym.addItems(["전체", "BTC만", "ETH만", "승리만", "패배만", "차단만"])
        self.cmb_sym.currentIndexChanged.connect(self._refresh_trades)
        self.cmb_period = QComboBox()
        self.cmb_period.addItems(["오늘", "7일", "30일", "전체"])
        self.cmb_period.currentIndexChanged.connect(self._refresh_trades)
        self.lbl_trade_summary = QLabel("총 0건 | 승률 —%")
        self.lbl_trade_summary.setStyleSheet(f"color:{C_ACCENT}; font-weight:bold;")
        filt.addWidget(self.cmb_sym)
        filt.addWidget(self.cmb_period)
        filt.addStretch()
        filt.addWidget(self.lbl_trade_summary)
        lay.addLayout(filt)

        # 거래 테이블
        cols = ["시간", "심볼", "방향", "진입가", "청산가", "PnL", "R값", "타입", "사유"]
        self.trade_table = QTableWidget(0, len(cols))
        self.trade_table.setHorizontalHeaderLabels(cols)
        hdr = self.trade_table.horizontalHeader()
        for i in range(len(cols) - 1):
            hdr.setSectionResizeMode(i, QHeaderView.ResizeToContents)
        hdr.setSectionResizeMode(len(cols) - 1, QHeaderView.Stretch)
        self.trade_table.setMinimumWidth(400)
        self.trade_table.setAlternatingRowColors(True)
        self.trade_table.verticalHeader().setVisible(False)
        self.trade_table.setColumnWidth(8, 400)
        lay.addWidget(self.trade_table, stretch=1)
        return w

    # ──────────── 탭 3: 분석 ────────────
    def _build_tab3(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)

        # 요약 카드
        cards = QHBoxLayout()
        self.a_total = _card("총 거래수", "0")
        self.a_wr = _card("승률", "—%")
        self.a_ev = _card("실측 EV(R)", "—")
        self.a_pf = _card("Profit Factor", "—")
        for c in (self.a_total, self.a_wr, self.a_ev, self.a_pf):
            cards.addWidget(c)
        lay.addLayout(cards)

        mid = QHBoxLayout()

        # SL 선점 분석
        gb_sl = QGroupBox("SL 선점 분석 — SL 후 가격 추적")
        sl_lay = QVBoxLayout(gb_sl)
        self.lbl_phantom = QLabel("데이터 없음")
        self.lbl_phantom.setWordWrap(True)
        self.lbl_phantom.setStyleSheet("font-size:12pt; line-height:1.6;")
        sl_lay.addWidget(self.lbl_phantom)
        mid.addWidget(gb_sl)

        # 차단 정확도
        gb_blk = QGroupBox("차단 시그널 추적")
        blk_lay = QVBoxLayout(gb_blk)
        self.lbl_blocked = QLabel("데이터 없음")
        self.lbl_blocked.setWordWrap(True)
        self.lbl_blocked.setStyleSheet("font-size:12pt; line-height:1.6;")
        blk_lay.addWidget(self.lbl_blocked)
        mid.addWidget(gb_blk)
        lay.addLayout(mid)

        # 레짐별 성과 테이블
        self.regime_table = QTableWidget(0, 5)
        self.regime_table.setHorizontalHeaderLabels(["레짐", "거래수", "승률", "평균R", "PF"])
        self.regime_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.regime_table.setAlternatingRowColors(True)
        self.regime_table.verticalHeader().setVisible(False)
        self.regime_table.setMaximumHeight(180)
        lay.addWidget(self.regime_table)
        lay.addStretch()
        return w

    # ════════════════════════════════════════
    # 갱신
    # ════════════════════════════════════════

    def _refresh(self) -> None:
        state = _load_state()
        trades = _load_trades()
        self._update_tab1(state, trades)
        self._refresh_trades()
        self._update_tab3(trades, state)

    # ── 탭 1 갱신 ──
    def _update_tab1(self, state: Dict, trades: List[Dict]) -> None:
        positions = state.get("positions", {})
        risk = state.get("risk_state", {})
        mdd = state.get("mdd_state", {})

        # 잔고: 마지막 거래의 equity_after 또는 198
        bal = 198.0
        if trades:
            for t in reversed(trades):
                ea = t.get("equity_after", 0)
                if ea:
                    bal = ea
                    break

        # 오늘 PnL
        today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        today_pnl = sum(
            t.get("pnl_usd", 0) for t in trades
            if t.get("time", "").startswith(today_str) and t.get("type") != "ENTRY"
        )

        initial = 198.0
        cum_pct = ((bal - initial) / initial * 100) if initial else 0

        risk_mode = risk.get("mode", "NORMAL")
        size_mult = risk.get("size_mult", 1.0)

        self._set_card(self.c_bal, f"${bal:,.2f}")
        pnl_sign = "+" if today_pnl >= 0 else ""
        pnl_col = C_GREEN if today_pnl >= 0 else C_RED
        self._set_card(self.c_pnl, f"{pnl_sign}${today_pnl:.2f}", pnl_col)
        cum_col = C_GREEN if cum_pct >= 0 else C_RED
        self._set_card(self.c_cum, f"{'+' if cum_pct >= 0 else ''}{cum_pct:.1f}%", cum_col)
        risk_col = C_GREEN if risk_mode == "NORMAL" else (C_RED if risk_mode in ("HARD", "STOP") else C_YELLOW)
        self._set_card(self.c_risk, f"{risk_mode} {size_mult}x", risk_col)

        # 포지션 테이블
        self.pos_table.setRowCount(0)
        if not positions:
            self.pos_table.setRowCount(1)
            item = _make_item("보유 포지션 없음")
            item.setForeground(QColor("#666"))
            self.pos_table.setSpan(0, 0, 1, 9)
            self.pos_table.setItem(0, 0, item)
            self.pos_table.setRowHeight(0, 35)
        else:
            for key, pos in positions.items():
                r = self.pos_table.rowCount()
                self.pos_table.insertRow(r)
                self.pos_table.setRowHeight(r, 35)
                sym = pos.get("symbol", key)
                side = pos.get("side", "?")
                regime = pos.get("regime", "?")
                strategy = pos.get("strategy", "?")
                avg = pos.get("avg_price", 0)
                # 상태 결정
                tp_count = pos.get("tp_count", 0)
                be = pos.get("be_activated", False)
                status = "보유"
                if tp_count >= 2:
                    status = "트레일▶"
                elif tp_count == 1:
                    status = "TP1✓"
                if be:
                    status = "BE◉" if tp_count == 0 else status

                items = [
                    _make_item(sym.split("/")[0] if "/" in sym else sym),
                    _make_item(side.upper(), fg=QColor(C_GREEN if side == "long" else C_RED)),
                    _make_item(regime),
                    _make_item(strategy),
                    _make_item(f"{avg:,.2f}"),
                    _make_item("—"),
                    _make_item("—"),
                    _make_item(str(pos.get("dca_count", 0))),
                    _make_item(status),
                ]
                if side == "long":
                    items[1].setBackground(QColor("#1b3a1b"))
                else:
                    items[1].setBackground(QColor("#3a1b1b"))
                for c, it in enumerate(items):
                    self.pos_table.setItem(r, c, it)

        # 하단 상태바 — 봇 미실행 시 기본 정보 표시
        bot_running = self.bot and self.bot.isRunning()
        if not bot_running:
            status_parts = []
            if CONFIG.PAPER_TRADING:
                status_parts.append("\U0001f4dd PAPER 모드")
            if not CONFIG.API_KEY:
                status_parts.append("\U0001f534 API 미설정")
            if not status_parts:
                status_parts.append("봇 미실행")
            regime_parts = []
            for sym in CONFIG.SYMBOLS:
                asset = "BTC" if "BTC" in sym else "ETH"
                regime_parts.append(f"{asset}: \u2014")
            self.lbl_regime.setText(
                "  |  ".join(regime_parts) + "    " + "  ".join(status_parts))

        daily_pct = (today_pnl / bal * 100) if bal else 0
        w_dd = mdd.get("weekly_dd", 0)
        m_dd = mdd.get("monthly_dd", 0)
        self.lbl_kill.setText(
            f"일간: {daily_pct:+.1f}% | MDD 주:{w_dd:+.1f}% 월:{m_dd:+.1f}%"
        )

    # ── 탭 2 갱신 ──
    def _refresh_trades(self) -> None:
        trades = _load_trades()
        filt = self.cmb_sym.currentText()
        period = self.cmb_period.currentText()

        # 기간 필터
        now = datetime.now(timezone.utc)
        if period == "오늘":
            cutoff = now.replace(hour=0, minute=0, second=0, microsecond=0)
        elif period == "7일":
            cutoff = now - timedelta(days=7)
        elif period == "30일":
            cutoff = now - timedelta(days=30)
        else:
            cutoff = None

        filtered = []
        for t in trades:
            ts = t.get("time", "")
            if cutoff:
                try:
                    tt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                    if tt < cutoff:
                        continue
                except (ValueError, TypeError):
                    continue
            if filt == "BTC만" and "BTC" not in t.get("symbol", ""):
                continue
            if filt == "ETH만" and "ETH" not in t.get("symbol", ""):
                continue
            if filt == "승리만" and t.get("pnl_usd", 0) <= 0:
                continue
            if filt == "패배만" and t.get("pnl_usd", 0) >= 0:
                continue
            if filt == "차단만" and t.get("type") != "BLOCKED":
                continue
            filtered.append(t)

        # 요약
        exits = [t for t in filtered if t.get("type") not in ("ENTRY", "DCA", "BLOCKED")]
        wins = sum(1 for t in exits if t.get("pnl_usd", 0) > 0)
        wr = (wins / len(exits) * 100) if exits else 0
        self.lbl_trade_summary.setText(f"총 {len(filtered)}건 | 승률 {wr:.1f}%")

        # 테이블
        self.trade_table.setRowCount(0)
        for t in reversed(filtered):  # 최신순
            r = self.trade_table.rowCount()
            self.trade_table.insertRow(r)
            self.trade_table.setRowHeight(r, 30)

            ts = t.get("time", "")[:19].replace("T", " ")
            sym = t.get("symbol", "").split("/")[0] if "/" in t.get("symbol", "") else t.get("symbol", "")
            side = t.get("side", "")
            entry = t.get("entry_price", 0)
            exit_p = t.get("exit_price") or 0
            pnl = t.get("pnl_usd", 0)
            r_val = t.get("r_value", 0)
            typ = t.get("type", "")
            reason = t.get("reason_detail", "") or t.get("reason", "")

            items = [
                _make_item(ts, Qt.AlignLeft | Qt.AlignVCenter),
                _make_item(sym),
                _make_item(side.upper(), fg=QColor(C_GREEN if side == "long" else C_RED)),
                _make_item(f"{entry:,.2f}" if entry else "—"),
                _make_item(f"{exit_p:,.2f}" if exit_p else "—"),
                _make_item(f"{'+'if pnl>=0 else ''}{pnl:.2f}", fg=_pnl_color(pnl)),
                _make_item(f"{r_val:+.2f}" if r_val else "—", fg=_pnl_color(r_val)),
                _make_item(typ),
                _make_item(reason, Qt.AlignLeft | Qt.AlignVCenter),
            ]
            # 타입 배경색
            tc = TYPE_COLORS.get(typ, C_GRAY)
            items[7].setBackground(QColor(tc))
            items[7].setForeground(QColor("#fff"))

            for c, it in enumerate(items):
                self.trade_table.setItem(r, c, it)

    # ── 탭 3 갱신 ──
    def _update_tab3(self, trades: List[Dict], state: Dict) -> None:
        exits = [t for t in trades if t.get("type") not in ("ENTRY", "DCA", "BLOCKED", None)]
        total = len(exits)
        wins = sum(1 for t in exits if t.get("pnl_usd", 0) > 0)
        wr = (wins / total * 100) if total else 0
        r_vals = [t.get("r_value", 0) for t in exits if t.get("r_value") is not None]
        avg_r = sum(r_vals) / len(r_vals) if r_vals else 0
        gross_w = sum(t["pnl_usd"] for t in exits if t.get("pnl_usd", 0) > 0)
        gross_l = sum(abs(t["pnl_usd"]) for t in exits if t.get("pnl_usd", 0) < 0)
        pf = gross_w / gross_l if gross_l > 0 else float("inf")

        self._set_card(self.a_total, str(total))
        self._set_card(self.a_wr, f"{wr:.1f}%", C_GREEN if wr >= 50 else C_RED)
        self._set_card(self.a_ev, f"{avg_r:+.3f}R", C_GREEN if avg_r >= 0 else C_RED)
        pf_str = f"{pf:.2f}" if pf != float("inf") else "∞"
        self._set_card(self.a_pf, pf_str, C_GREEN if pf >= 1 else C_RED)

        # SL 선점
        phantom = state.get("phantom_stats") or {}
        if phantom and phantom.get("total", 0) > 0:
            pt = phantom["total"]
            prem = phantom.get("premature_count", 0)
            corr = phantom.get("correct_count", 0)
            avg_fav = phantom.get("avg_peak_favorable_pct", 0)
            rec = "SL 배수 적절" if corr / pt > 0.5 else "SL 확대 검토"
            self.lbl_phantom.setText(
                f'<span style="color:{C_RED}">선점(TP갔었음): {prem}건 / {pt}건 = {prem/pt*100:.1f}%</span><br>'
                f'<span style="color:{C_GREEN}">정확(더떨어짐): {corr}건 / {pt}건 = {corr/pt*100:.1f}%</span><br>'
                f'평균 최대 반등: +{avg_fav:.2f}%<br>'
                f'<b>★ 권고: {rec}</b>'
            )
        else:
            self.lbl_phantom.setText("SL 선점 데이터 없음")

        # 차단 정확도
        blocked = state.get("blocked_stats") or {}
        if blocked and blocked.get("total", 0) > 0:
            bt = blocked["total"]
            bc = blocked.get("correct_block_count", 0)
            bw = blocked.get("wrong_block_count", 0)
            rec2 = "현 수준 유지" if bc / bt > 0.5 else "완화 검토"
            self.lbl_blocked.setText(
                f'<span style="color:{C_GREEN}">차단 정확: {bc}건 / {bt}건 = {bc/bt*100:.1f}%</span><br>'
                f'<span style="color:{C_RED}">차단 오류: {bw}건 / {bt}건 = {bw/bt*100:.1f}%</span><br>'
                f'<b>★ 쿨다운 판정: {rec2}</b>'
            )
        else:
            self.lbl_blocked.setText("차단 시그널 데이터 없음")

        # 레짐별 성과
        regime_data: Dict[str, Dict] = {}
        for t in exits:
            reg = t.get("regime", "?") or "?"
            if reg not in regime_data:
                regime_data[reg] = {"trades": 0, "wins": 0, "r_sum": 0, "gw": 0, "gl": 0}
            d = regime_data[reg]
            d["trades"] += 1
            pnl_u = t.get("pnl_usd", 0)
            rv = t.get("r_value", 0)
            d["r_sum"] += rv
            if pnl_u > 0:
                d["wins"] += 1
                d["gw"] += pnl_u
            else:
                d["gl"] += abs(pnl_u)

        self.regime_table.setRowCount(0)
        for reg, d in sorted(regime_data.items()):
            r = self.regime_table.rowCount()
            self.regime_table.insertRow(r)
            wr_r = d["wins"] / d["trades"] * 100 if d["trades"] else 0
            avg_r_r = d["r_sum"] / d["trades"] if d["trades"] else 0
            pf_r = d["gw"] / d["gl"] if d["gl"] > 0 else float("inf")
            pf_s = f"{pf_r:.2f}" if pf_r != float("inf") else "∞"
            items = [
                _make_item(reg),
                _make_item(str(d["trades"])),
                _make_item(f"{wr_r:.1f}%", fg=QColor(C_GREEN if wr_r >= 50 else C_RED)),
                _make_item(f"{avg_r_r:+.3f}", fg=QColor(C_GREEN if avg_r_r >= 0 else C_RED)),
                _make_item(pf_s, fg=QColor(C_GREEN if pf_r >= 1 else C_RED)),
            ]
            for c, it in enumerate(items):
                self.regime_table.setItem(r, c, it)

    # ──────────── 탭 4: 백테스트 ────────────
    def _build_tab4(self) -> QWidget:
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        inner = QWidget()
        lay = QVBoxLayout(inner)

        # 설정 패널
        cfg = QHBoxLayout()
        self.bt_sym = QComboBox()
        self.bt_sym.addItems(["BTC/USDT:USDT", "ETH/USDT:USDT"])
        self.bt_period = QComboBox()
        self.bt_period.addItems(["1개월", "3개월", "6개월", "12개월"])
        self.bt_period.setCurrentIndex(1)
        self.bt_bal = QDoubleSpinBox()
        self.bt_bal.setRange(10, 100000)
        self.bt_bal.setValue(198.0)
        self.bt_bal.setPrefix("$ ")
        btn_run = QPushButton("▶ 백테스트 실행")
        btn_run.setStyleSheet(f"background:{C_HEADER}; padding:8px 16px; border-radius:4px;")
        btn_run.clicked.connect(self._run_backtest)
        btn_stop = QPushButton("■ 중지")
        btn_stop.setStyleSheet(f"background:{C_RED}; padding:8px 12px; border-radius:4px;")
        btn_stop.clicked.connect(self._stop_backtest)
        for w in [QLabel("심볼:"), self.bt_sym, QLabel("기간:"), self.bt_period,
                   QLabel("잔고:"), self.bt_bal, btn_run, btn_stop]:
            cfg.addWidget(w)
        cfg.addStretch()
        lay.addLayout(cfg)

        # 진행바
        self.bt_progress = QProgressBar()
        self.bt_progress.setMaximumHeight(20)
        self.bt_status = QLabel("대기중")
        self.bt_status.setStyleSheet("color:#999;")
        lay.addWidget(self.bt_progress)
        lay.addWidget(self.bt_status)

        # 결과 영역 (숨김)
        self.bt_result_area = QWidget()
        rl = QVBoxLayout(self.bt_result_area)

        # 결과 카드 6개
        cards = QHBoxLayout()
        self.bt_c = {}
        for key, title in [("trades", "총거래수"), ("wr", "승률"), ("pf", "PF"),
                           ("ev", "실측EV"), ("mdd", "MDD"), ("sharpe", "Sharpe")]:
            c = _card(title, "—")
            self.bt_c[key] = c
            cards.addWidget(c)
        rl.addLayout(cards)

        # Equity curve
        self.bt_fig = Figure(figsize=(10, 2.5), dpi=100)
        self.bt_fig.patch.set_facecolor("none")
        self.bt_ax = self.bt_fig.add_subplot(111)
        self.bt_canvas = FigureCanvasQTAgg(self.bt_fig)
        self.bt_canvas.setMinimumHeight(250)
        self.bt_canvas.setMaximumHeight(250)
        rl.addWidget(self.bt_canvas)

        # TP 분포
        self.bt_tp_lay = QVBoxLayout()
        gb_tp = QGroupBox("TP 분포")
        gb_tp.setLayout(self.bt_tp_lay)
        rl.addWidget(gb_tp)

        # 추가 분석
        gb_extra = QGroupBox("추가 분석")
        el = QVBoxLayout(gb_extra)
        self.bt_extra = QLabel("—")
        self.bt_extra.setWordWrap(True)
        self.bt_extra.setStyleSheet("font-size:12pt;")
        el.addWidget(self.bt_extra)
        rl.addWidget(gb_extra)

        # 레짐별 성과
        self.bt_regime_table = QTableWidget(0, 5)
        self.bt_regime_table.setHorizontalHeaderLabels(["레짐", "거래수", "승률", "평균R", "PF"])
        self.bt_regime_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.bt_regime_table.setAlternatingRowColors(True)
        self.bt_regime_table.verticalHeader().setVisible(False)
        self.bt_regime_table.setMaximumHeight(160)
        rl.addWidget(self.bt_regime_table)

        # 최적화 섹션
        gb_opt = QGroupBox("🔧 파라미터 자동 최적화")
        ol = QVBoxLayout(gb_opt)
        opt_row = QHBoxLayout()
        btn_opt = QPushButton("⚡ 최적화 실행")
        btn_opt.setStyleSheet(f"background:{C_ACCENT}; padding:8px 16px; border-radius:4px;")
        btn_opt.clicked.connect(self._run_optimizer)
        btn_opt_stop = QPushButton("■ 중지")
        btn_opt_stop.setStyleSheet(f"background:{C_RED}; padding:8px 12px; border-radius:4px;")
        btn_opt_stop.clicked.connect(self._stop_optimizer)
        self.bt_opt_progress = QProgressBar()
        self.bt_opt_progress.setMaximumHeight(18)
        self.bt_opt_status = QLabel("")
        opt_row.addWidget(btn_opt)
        opt_row.addWidget(btn_opt_stop)
        opt_row.addWidget(self.bt_opt_progress)
        opt_row.addWidget(self.bt_opt_status)
        ol.addLayout(opt_row)

        # 결과 테이블
        self.bt_opt_table = QTableWidget(0, 4)
        self.bt_opt_table.setHorizontalHeaderLabels(["파라미터", "기존값", "최적값", "변화"])
        self.bt_opt_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.bt_opt_table.setAlternatingRowColors(True)
        self.bt_opt_table.verticalHeader().setVisible(False)
        self.bt_opt_table.setMaximumHeight(180)
        ol.addWidget(self.bt_opt_table)

        # 적용/복원 버튼
        opt_btn_row = QHBoxLayout()
        self.btn_apply_opt = QPushButton("✅ 최적값 적용")
        self.btn_apply_opt.setStyleSheet(f"background:{C_GREEN}; color:black; padding:8px 16px; border-radius:4px;")
        self.btn_apply_opt.clicked.connect(self._apply_optimizer)
        self.btn_apply_opt.setEnabled(False)
        self.btn_restore_opt = QPushButton("↩ 원래값 복원")
        self.btn_restore_opt.clicked.connect(self._restore_optimizer)
        self.btn_restore_opt.setEnabled(False)
        opt_btn_row.addWidget(self.btn_apply_opt)
        opt_btn_row.addWidget(self.btn_restore_opt)
        opt_btn_row.addStretch()
        ol.addLayout(opt_btn_row)

        warn = QLabel("※ 위 수치는 경보선(재테스트 후보)이며 자동 변경 규칙이 아닙니다.")
        warn.setStyleSheet(f"color:{C_YELLOW}; font-size:11pt; padding:4px;")
        ol.addWidget(warn)
        rl.addWidget(gb_opt)

        self.bt_result_area.setVisible(False)
        lay.addWidget(self.bt_result_area)
        lay.addStretch()

        scroll.setWidget(inner)
        return scroll

    def _run_backtest(self) -> None:
        from fang_v10.dashboard_tabs import BacktestThread
        if self._bt_thread and self._bt_thread.isRunning():
            return
        months = {"1개월": 1, "3개월": 3, "6개월": 6, "12개월": 12}[self.bt_period.currentText()]
        self._bt_thread = BacktestThread(self.bt_sym.currentText(), months, self.bt_bal.value())
        self._bt_thread.progress.connect(self._on_bt_progress)
        self._bt_thread.finished.connect(self._on_bt_finished)
        self._bt_thread.error.connect(lambda e: self.bt_status.setText(f"오류: {e}"))
        self.bt_progress.setValue(0)
        self.bt_status.setText("시작중...")
        self.bt_result_area.setVisible(False)
        self._bt_thread.start()

    def _stop_backtest(self) -> None:
        if self._bt_thread and self._bt_thread.isRunning():
            self._bt_thread.stop()
            self.bt_status.setText("중지됨")

    def _on_bt_progress(self, pct: int, msg: str) -> None:
        self.bt_progress.setValue(pct)
        self.bt_status.setText(msg)

    def _on_bt_finished(self, result) -> None:
        self._bt_last_result = result
        if self._bt_thread and hasattr(self._bt_thread, 'result_df'):
            self._last_bt_df = self._bt_thread.result_df
            self._last_bt_symbol = self._bt_thread.symbol
        r = result
        self._set_card(self.bt_c["trades"], str(r.total_trades))
        self._set_card(self.bt_c["wr"], f"{r.winrate*100:.1f}%",
                       C_GREEN if r.winrate >= 0.5 else C_RED)
        pf_s = f"{r.profit_factor:.2f}" if r.profit_factor != float("inf") else "∞"
        self._set_card(self.bt_c["pf"], pf_s, C_GREEN if r.profit_factor >= 1 else C_RED)
        self._set_card(self.bt_c["ev"], f"{r.avg_r:+.3f}R",
                       C_GREEN if r.avg_r >= 0 else C_RED)
        self._set_card(self.bt_c["mdd"], f"{r.max_drawdown*100:.1f}%", C_RED)
        # Sharpe 근사
        if r.equity_curve and len(r.equity_curve) > 2:
            import numpy as np
            rets = np.diff(r.equity_curve) / np.array(r.equity_curve[:-1])
            sharpe = float(np.mean(rets) / np.std(rets) * np.sqrt(252)) if np.std(rets) > 0 else 0
        else:
            sharpe = 0
        self._set_card(self.bt_c["sharpe"], f"{sharpe:.2f}",
                       C_GREEN if sharpe >= 1 else C_YELLOW if sharpe >= 0 else C_RED)

        # Equity curve
        self.bt_ax.clear()
        self.bt_ax.plot(r.equity_curve, color=C_GREEN, linewidth=1.5)
        self.bt_ax.set_facecolor("#0a0a1a")
        self.bt_ax.tick_params(colors="white", labelsize=9)
        self.bt_ax.set_xlabel("거래", color="white", fontsize=10)
        self.bt_ax.set_ylabel("잔고($)", color="white", fontsize=10)
        for spine in self.bt_ax.spines.values():
            spine.set_color("#333")
        self.bt_fig.tight_layout()
        self.bt_canvas.draw()

        # TP 분포
        while self.bt_tp_lay.count():
            child = self.bt_tp_lay.takeAt(0)
            if child.widget():
                child.widget().deleteLater()
        tp = r.tp_stats or {}
        max_v = max(tp.values()) if tp else 1
        bar_colors = {"TP1": C_GREEN, "TP2": C_GREEN, "TRAIL": C_YELLOW,
                      "SL": C_RED, "BE": C_YELLOW, "EARLY": C_ORANGE,
                      "TIME": C_ORANGE, "TREND_REV": C_ORANGE, "EMERGENCY": C_RED}
        for key in ["TP1", "TP2", "TRAIL", "SL", "BE", "EARLY", "TIME", "TREND_REV"]:
            v = tp.get(key, 0)
            if v == 0:
                continue
            row = QHBoxLayout()
            lbl = QLabel(f"{key}:")
            lbl.setFixedWidth(80)
            bar = QProgressBar()
            bar.setMaximum(max_v)
            bar.setValue(v)
            bar.setFormat(f"{v}건")
            bc = bar_colors.get(key, C_GRAY)
            bar.setStyleSheet(f"QProgressBar::chunk {{ background:{bc}; }}"
                              f"QProgressBar {{ background:{C_BG}; border:none; color:white; }}")
            bar.setMaximumHeight(22)
            row.addWidget(lbl)
            row.addWidget(bar)
            w = QWidget()
            w.setLayout(row)
            self.bt_tp_lay.addWidget(w)

        # 추가 분석
        def _c(val, thresh, bad_color, good_color=C_GREEN):
            return bad_color if val >= thresh else good_color
        be_c = _c(r.be_exit_rate, 0.4, C_RED)
        tp_ext_c = C_GREEN if r.tp_extension_rate > 0.6 else C_TEXT
        sbc = _c(r.same_bar_conflict_rate, 0.05, C_YELLOW, C_TEXT)
        box_n = (r.regime_stats or {}).get("BOX", {}).get("trades", 0)
        box_c = C_RED if box_n < 10 else C_TEXT
        ph = r.sl_phantom_stats or {}
        ph_pct = ph.get("premature_pct", 0)
        ph_rec = r.sl_phantom_recommendation or ""
        bl = r.blocked_signal_stats or {}
        bl_pct = bl.get("correct_block_pct", 0)
        self.bt_extra.setText(
            f'<span style="color:{be_c}">BE이탈률: {r.be_exit_rate*100:.1f}%</span>'
            f'{" ⚠ 40% 초과" if r.be_exit_rate > 0.4 else ""}<br>'
            f'<span style="color:{tp_ext_c}">TP연장률: {r.tp_extension_rate*100:.1f}%</span><br>'
            f'<span style="color:{sbc}">same-bar 충돌: {r.same_bar_conflict_rate*100:.1f}%</span>'
            f'{" ⚠" if r.same_bar_conflict_rate >= 0.05 else ""}<br>'
            f'<span style="color:{box_c}">BOX 거래수: {box_n}건</span>'
            f'{" ⚠ 10건 미만" if box_n < 10 else ""}<br>'
            f'SL 선점률: {ph_pct:.1f}% {ph_rec}<br>'
            f'차단 정확도: {bl_pct:.1f}%<br>'
            f'<br><b>== 진단 ==</b><br>'
            f'레짐분포: {r.diag_regime_bars}<br>'
            f'시그널생성: {r.diag_signals_generated}건<br>'
            f'포지션보유중차단: {r.diag_blocked_has_pos}건<br>'
            f'쿨다운차단: {r.diag_blocked_cooldown}건<br>'
            f'사이징거부: {r.diag_blocked_sizing}건<br>'
            f'킬스위치차단: {r.diag_blocked_killswitch}건'
        )

        # 레짐별 성과
        self.bt_regime_table.setRowCount(0)
        for reg, d in sorted((r.regime_stats or {}).items()):
            row = self.bt_regime_table.rowCount()
            self.bt_regime_table.insertRow(row)
            trades_n = d.get("trades", 0)
            pnl_r = d.get("pnl", 0)
            wr_r = 0
            avg_r_r = 0
            pf_r = 0
            self.bt_regime_table.setItem(row, 0, _make_item(reg))
            self.bt_regime_table.setItem(row, 1, _make_item(str(trades_n)))
            self.bt_regime_table.setItem(row, 2, _make_item("—"))
            self.bt_regime_table.setItem(row, 3, _make_item("—"))
            self.bt_regime_table.setItem(row, 4, _make_item(f"${pnl_r:.2f}",
                                         fg=_pnl_color(pnl_r)))

        self.bt_result_area.setVisible(True)

    def _run_optimizer(self) -> None:
        if self._last_bt_df is None:
            from PyQt5.QtWidgets import QMessageBox
            QMessageBox.warning(self, "경고", "먼저 백테스트를 실행해주세요.")
            return
        from fang_v10.dashboard_tabs import OptimizerThread
        self._opt_thread = OptimizerThread(
            self._last_bt_df, self._last_bt_symbol or self.bt_sym.currentText(),
            self.bt_bal.value())
        self._opt_thread.progress.connect(self._on_opt_progress)
        self._opt_thread.finished.connect(self._on_opt_finished)
        self._opt_thread.error.connect(lambda e: self.bt_opt_status.setText(f"오류: {e}"))
        self.bt_opt_progress.setValue(0)
        self.bt_opt_status.setText("최적화 실행중...")
        self._opt_thread.start()

    def _stop_optimizer(self) -> None:
        if hasattr(self, '_opt_thread') and self._opt_thread and self._opt_thread.isRunning():
            self._opt_thread.stop()
            self.bt_opt_status.setText("중지됨")

    def _on_opt_progress(self, pct: int, msg: str) -> None:
        self.bt_opt_progress.setValue(pct)
        self.bt_opt_status.setText(msg)

    def _on_opt_finished(self, result) -> None:
        self._opt_result = result
        if not result or not result.get("best_params"):
            self.bt_opt_status.setText("유효한 최적 조합 없음")
            return

        best = result["best_params"]
        # 현재값 가져오기
        current_vals = {}
        for k in best:
            if k.startswith("SL_ATR_MULT_"):
                asset = k.split("_")[-1]
                current_vals[k] = CONFIG.SL_ATR_MULT.get(asset, 0)
            else:
                current_vals[k] = getattr(CONFIG, k, 0)

        # 테이블 채우기
        self.bt_opt_table.setRowCount(0)
        for param, new_val in best.items():
            row = self.bt_opt_table.rowCount()
            self.bt_opt_table.insertRow(row)
            old_val = current_vals.get(param, "?")
            diff = ""
            if isinstance(new_val, (int, float)) and isinstance(old_val, (int, float)):
                d = new_val - old_val
                diff = f"{d:+.2f}"
            self.bt_opt_table.setItem(row, 0, _make_item(param))
            self.bt_opt_table.setItem(row, 1, _make_item(f"{old_val}"))
            self.bt_opt_table.setItem(row, 2, _make_item(f"{new_val}",
                                      fg=QColor(C_GREEN)))
            self.bt_opt_table.setItem(row, 3, _make_item(diff,
                                      fg=_pnl_color(float(diff)) if diff else QColor(C_TEXT)))

        # 요약
        self.bt_opt_status.setText(
            f"완료: PF={result['best_pf']:.2f} EV={result['best_ev']:+.3f}R "
            f"MDD={result['best_mdd']*100:.1f}%"
        )
        self.btn_apply_opt.setEnabled(True)
        self.btn_restore_opt.setEnabled(True)
        # 원래값 저장
        self._opt_originals = current_vals

    def _apply_optimizer(self) -> None:
        if not hasattr(self, '_opt_result') or not self._opt_result:
            return
        from fang_v10.optimizer import AutoOptimizer
        opt = AutoOptimizer()
        opt.apply_best(self._opt_result)
        self.bt_opt_status.setText("✅ 최적값 적용 완료!")
        QMessageBox.information(self, "적용", "최적 파라미터가 적용되었습니다.")

    def _restore_optimizer(self) -> None:
        if not hasattr(self, '_opt_originals'):
            return
        for k, v in self._opt_originals.items():
            if k.startswith("SL_ATR_MULT_"):
                asset = k.split("_")[-1]
                CONFIG.SL_ATR_MULT[asset] = v
            else:
                setattr(CONFIG, k, v)
        self.bt_opt_status.setText("↩ 원래값 복원 완료")
        QMessageBox.information(self, "복원", "원래 파라미터로 복원되었습니다.")

    # ──────────── 탭 5: 설정 ────────────
    def _build_tab5(self) -> QWidget:
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        inner = QWidget()
        lay = QVBoxLayout(inner)

        # ── 섹션 1: API 연결 ──
        gb_api = QGroupBox("🔌 API 연결")
        al = QGridLayout(gb_api)
        self.api_key = QLineEdit()
        self.api_key.setEchoMode(QLineEdit.Password)
        self.api_key.setPlaceholderText("Bitget API Key")
        self.api_secret = QLineEdit()
        self.api_secret.setEchoMode(QLineEdit.Password)
        self.api_secret.setPlaceholderText("Bitget Secret")
        self.api_pass = QLineEdit()
        self.api_pass.setEchoMode(QLineEdit.Password)
        self.api_pass.setPlaceholderText("Bitget Passphrase")
        al.addWidget(QLabel("API Key:"), 0, 0)
        al.addWidget(self.api_key, 0, 1)
        al.addWidget(QLabel("Secret:"), 1, 0)
        al.addWidget(self.api_secret, 1, 1)
        al.addWidget(QLabel("Passphrase:"), 2, 0)
        al.addWidget(self.api_pass, 2, 1)
        btn_row = QHBoxLayout()
        btn_save_api = QPushButton("💾 저장")
        btn_save_api.clicked.connect(self._save_api)
        btn_test = QPushButton("🔌 연결 테스트")
        btn_test.clicked.connect(self._test_api)
        self.lbl_api_status = QLabel("🔴 미연결")
        self.lbl_api_status.setStyleSheet("font-size:14pt; font-weight:bold;")
        btn_row.addWidget(btn_save_api)
        btn_row.addWidget(btn_test)
        btn_row.addWidget(self.lbl_api_status)
        btn_row.addStretch()
        al.addLayout(btn_row, 3, 0, 1, 2)
        lay.addWidget(gb_api)

        # ── 섹션 2: 봇 제어 ──
        gb_bot = QGroupBox("🤖 봇 제어")
        bl = QHBoxLayout(gb_bot)
        btn_start = QPushButton("▶ 시작")
        btn_start.setStyleSheet(f"background:{C_GREEN}; color:black; padding:8px 16px; border-radius:4px;")
        btn_start.clicked.connect(self._start_bot)
        btn_stop_bot = QPushButton("■ 정지")
        btn_stop_bot.setStyleSheet(f"background:{C_RED}; padding:8px 16px; border-radius:4px;")
        btn_stop_bot.clicked.connect(self._stop_bot)
        btn_restart = QPushButton("⟳ 재시작")
        btn_restart.clicked.connect(self._restart_bot)
        self.lbl_mode = QLabel("📝 PAPER" if CONFIG.PAPER_TRADING else "🔴 LIVE")
        if not CONFIG.PAPER_TRADING:
            self.lbl_mode.setStyleSheet(f"color:{C_RED}; font-weight:bold;")
        self.lbl_bot_status = QLabel("정지됨")
        bl.addWidget(btn_start)
        bl.addWidget(btn_stop_bot)
        bl.addWidget(btn_restart)
        bl.addWidget(self.lbl_mode)
        bl.addWidget(self.lbl_bot_status)
        bl.addStretch()
        lay.addWidget(gb_bot)

        # ── 섹션 3: 트레이딩 파라미터 ──
        gb_param = QGroupBox("📊 트레이딩 파라미터")
        gl = QGridLayout(gb_param)
        self._spins: Dict[str, Any] = {}

        def _dspin(label, key, val, lo, hi, step, row, col):
            gl.addWidget(QLabel(label), row, col * 2)
            sp = QDoubleSpinBox()
            sp.setRange(lo, hi)
            sp.setSingleStep(step)
            sp.setValue(val)
            gl.addWidget(sp, row, col * 2 + 1)
            self._spins[key] = sp

        def _ispin(label, key, val, lo, hi, row, col):
            gl.addWidget(QLabel(label), row, col * 2)
            sp = QSpinBox()
            sp.setRange(lo, hi)
            sp.setValue(val)
            gl.addWidget(sp, row, col * 2 + 1)
            self._spins[key] = sp

        # 컬럼 0: 리스크
        gl.addWidget(QLabel("── 리스크 ──"), 0, 0, 1, 2)
        _dspin("1R 비율(%):", "RISK_PER_TRADE_PCT", CONFIG.RISK_PER_TRADE_PCT, 0.1, 5.0, 0.1, 1, 0)
        _dspin("SL배수 BTC:", "SL_BTC", CONFIG.SL_ATR_MULT.get("BTC", 1.5), 0.5, 5.0, 0.1, 2, 0)
        _dspin("SL배수 ETH:", "SL_ETH", CONFIG.SL_ATR_MULT.get("ETH", 1.8), 0.5, 5.0, 0.1, 3, 0)
        _dspin("DCA ATR배수:", "DCA_ATR_MULT", CONFIG.DCA_ATR_MULT, 0.5, 5.0, 0.1, 4, 0)

        # 컬럼 1: TP/SL
        gl.addWidget(QLabel("── TP/SL ──"), 0, 2, 1, 2)
        _dspin("TP1 R:", "TP1_R", CONFIG.TP1_R, 0.3, 5.0, 0.1, 1, 1)
        _dspin("TP1 비율(%):", "TP1_RATIO", CONFIG.TP1_RATIO * 100, 10, 80, 5, 2, 1)
        _dspin("TP2 R:", "TP2_R", CONFIG.TP2_R, 0.5, 5.0, 0.1, 3, 1)
        _dspin("TP2 비율(%):", "TP2_RATIO", CONFIG.TP2_RATIO * 100, 10, 80, 5, 4, 1)
        _dspin("트레일 R:", "TP3_TRAIL_R", CONFIG.TP3_TRAIL_R, 0.1, 3.0, 0.1, 5, 1)
        _dspin("BE R:", "BE_TRIGGER_R", CONFIG.BE_TRIGGER_R, 0.3, 3.0, 0.1, 6, 1)

        # 컬럼 2: 쿨다운/킬스위치
        gl.addWidget(QLabel("── 쿨다운/킬스위치 ──"), 0, 4, 1, 2)
        _ispin("일반(초):", "COOLDOWN_NORMAL_SEC", CONFIG.COOLDOWN_NORMAL_SEC, 10, 600, 1, 2)
        _ispin("SL후(초):", "COOLDOWN_AFTER_SL_SEC", CONFIG.COOLDOWN_AFTER_SL_SEC, 60, 1800, 2, 2)
        _ispin("SL+같은방향(초):", "COOLDOWN_AFTER_SL_SAME_DIR_SEC",
               CONFIG.COOLDOWN_AFTER_SL_SAME_DIR_SEC, 60, 3600, 3, 2)
        _dspin("일간 SOFT(%):", "DAILY_LOSS_SOFT", CONFIG.DAILY_LOSS_SOFT, 0.5, 5.0, 0.1, 4, 2)
        _dspin("일간 HARD(%):", "DAILY_LOSS_HARD", CONFIG.DAILY_LOSS_HARD, 0.5, 5.0, 0.1, 5, 2)
        _dspin("일간 STOP(%):", "DAILY_LOSS_STOP", CONFIG.DAILY_LOSS_STOP, 1.0, 10.0, 0.1, 6, 2)
        _dspin("월간 MDD(%):", "MONTHLY_MDD_LIMIT", CONFIG.MONTHLY_MDD_LIMIT, 1.0, 20.0, 0.5, 7, 2)

        lay.addWidget(gb_param)

        # 저장 버튼
        save_row = QHBoxLayout()
        btn_save_cfg = QPushButton("💾 설정 저장")
        btn_save_cfg.setStyleSheet(f"background:{C_HEADER}; padding:10px 24px; border-radius:4px;")
        btn_save_cfg.clicked.connect(self._save_params)
        save_row.addWidget(btn_save_cfg)
        save_row.addWidget(QLabel("★ 저장 시 봇 재시작 없이 즉시 반영됩니다"))
        save_row.addStretch()
        lay.addLayout(save_row)
        lay.addStretch()

        scroll.setWidget(inner)
        return scroll

    # ── 탭5 핸들러 ──
    def _save_api(self) -> None:
        env_path = os.path.join(os.path.dirname(__file__), "..", ".env")
        lines: list = []
        if os.path.exists(env_path):
            with open(env_path, "r") as f:
                lines = f.readlines()
        keys = {
            "BITGET_API_KEY": self.api_key.text(),
            "BITGET_API_SECRET": self.api_secret.text(),
            "BITGET_PASSPHRASE": self.api_pass.text(),
        }
        for k, v in keys.items():
            found = False
            for i, line in enumerate(lines):
                if line.startswith(k + "="):
                    lines[i] = f"{k}={v}\n"
                    found = True
                    break
            if not found:
                lines.append(f"{k}={v}\n")
        with open(env_path, "w") as f:
            f.writelines(lines)
        CONFIG.reload_env()
        QMessageBox.information(self, "저장", "API 키가 저장되었습니다.")

    def _test_api(self) -> None:
        from PyQt5.QtCore import QThread as _QT, pyqtSignal as _sig

        class _ApiTestThread(_QT):
            result = _sig(str)

            def run(self_t):
                try:
                    from fang_v10.exchange_api import BitgetClient
                    client = BitgetClient(
                        CONFIG.API_KEY, CONFIG.API_SECRET,
                        CONFIG.PASSPHRASE, paper=False,
                    )
                    bal = client.fetch_balance()
                    self_t.result.emit(f"🟢 연결 성공! 잔고: ${bal:.2f}")
                except Exception as e:
                    self_t.result.emit(f"🔴 연결 실패: {e}")

        def _on_result(msg: str):
            self.lbl_api_status.setText(msg)
            if "성공" in msg:
                QMessageBox.information(self, "성공", msg)
            else:
                QMessageBox.warning(self, "실패", msg)

        self.lbl_api_status.setText("테스트 중...")
        self._api_test = _ApiTestThread(self)
        self._api_test.result.connect(_on_result)
        self._api_test.start()

    def _start_bot(self) -> None:
        from fang_v10.dashboard_tabs import BotThread
        if self.bot and self.bot.isRunning():
            return
        self.bot = BotThread()
        self.bot.state_updated.connect(self._on_bot_state)
        self.bot.trade_executed.connect(self._on_bot_trade)
        self.bot.error_occurred.connect(self._on_bot_error)
        self.bot.log_message.connect(self._on_bot_log)
        self._bot_start_time = _time.time()
        self.bot.start()
        self.lbl_bot_status.setText("실행중")

    def _stop_bot(self) -> None:
        if self.bot:
            self.bot.stop()
            self.lbl_bot_status.setText("정지됨")

    def _restart_bot(self) -> None:
        self._stop_bot()
        QTimer.singleShot(1000, self._start_bot)

    def _on_bot_state(self, data: dict) -> None:
        elapsed = int(_time.time() - self._bot_start_time)
        m, s = divmod(elapsed, 60)
        self.lbl_bot_status.setText(f"실행중 ⏱ {m}분 {s:02d}초")

        # ── 카드 갱신 ──
        balance = data.get("balance", 0)
        daily_pnl = data.get("daily_pnl", 0)
        risk_mode = data.get("risk_mode", "NORMAL")
        size_mult = data.get("size_mult", 1.0)

        self._set_card(self.c_bal, f"${balance:,.2f}")
        pnl_sign = "+" if daily_pnl >= 0 else ""
        pnl_col = C_GREEN if daily_pnl >= 0 else C_RED
        self._set_card(self.c_pnl, f"{pnl_sign}${daily_pnl:.2f}", pnl_col)
        initial = 198.0
        cum_pct = ((balance - initial) / initial * 100) if initial else 0
        cum_col = C_GREEN if cum_pct >= 0 else C_RED
        self._set_card(self.c_cum, f"{'+' if cum_pct >= 0 else ''}{cum_pct:.1f}%", cum_col)
        risk_col = C_GREEN if risk_mode == "NORMAL" else (
            C_RED if risk_mode in ("HARD", "STOP") else C_YELLOW)
        self._set_card(self.c_risk, f"{risk_mode} {size_mult}x", risk_col)

        # ── 레짐 표시 갱신 ──
        _REGIME_DISPLAY = {
            "TREND": ("TREND \u25b2", C_GREEN),
            "BOX": ("BOX \u2550", "#ffc107"),
            "PROTECT": ("PROTECT \u26a0", C_RED),
        }
        regimes = data.get("regimes", {})
        regime_parts = []
        for sym in CONFIG.SYMBOLS:
            asset = "BTC" if "BTC" in sym else "ETH"
            regime_val = regimes.get(sym, "\u2014")
            display, color = _REGIME_DISPLAY.get(regime_val, (regime_val, C_ACCENT))
            regime_parts.append(
                f"<span style='color:{color}'>{asset}: [{display}]</span>")
        self.lbl_regime.setText("  |  ".join(regime_parts))

        # ── 포지션 테이블 갱신 ──
        positions = data.get("positions", {})
        self.pos_table.setRowCount(0)
        if not positions:
            self.pos_table.setRowCount(1)
            item = _make_item("보유 포지션 없음")
            item.setForeground(QColor("#666"))
            self.pos_table.setSpan(0, 0, 1, 9)
            self.pos_table.setItem(0, 0, item)
            self.pos_table.setRowHeight(0, 35)
        else:
            for key, pos in positions.items():
                r = self.pos_table.rowCount()
                self.pos_table.insertRow(r)
                self.pos_table.setRowHeight(r, 35)
                sym = pos.get("symbol", key)
                side = pos.get("side", "?")
                regime = pos.get("regime", "?")
                strategy = pos.get("strategy", "?")
                avg = pos.get("avg_price", 0)
                tp_count = pos.get("tp_count", 0)
                be = pos.get("be_activated", False)
                status = "보유"
                if tp_count >= 2:
                    status = "트레일\u25b6"
                elif tp_count == 1:
                    status = "TP1\u2713"
                if be and tp_count == 0:
                    status = "BE\u25c9"
                items = [
                    _make_item(sym.split("/")[0] if "/" in sym else sym),
                    _make_item(side.upper(),
                               fg=QColor(C_GREEN if side == "long" else C_RED)),
                    _make_item(regime),
                    _make_item(strategy),
                    _make_item(f"{avg:,.2f}"),
                    _make_item("\u2014"),   # 현재R — 실시간 가격 없이는 계산 불가
                    _make_item("\u2014"),   # PnL($)
                    _make_item(str(pos.get("dca_count", 0))),
                    _make_item(status),
                ]
                if side == "long":
                    items[1].setBackground(QColor("#1b3a1b"))
                else:
                    items[1].setBackground(QColor("#3a1b1b"))
                for c, it in enumerate(items):
                    self.pos_table.setItem(r, c, it)

    def _on_bot_trade(self, trade: dict) -> None:
        symbol = trade.get("symbol", "?")
        ttype = trade.get("type", "?")
        reason = trade.get("reason", "")
        side = trade.get("side", "")
        price = trade.get("price", 0)
        pnl = trade.get("pnl", None)

        parts = [f"거래: {ttype} {symbol}"]
        if side:
            parts.append(side.upper())
        if price:
            parts.append(f"@{price:.2f}")
        if pnl is not None:
            parts.append(f"PnL={'+'if pnl>=0 else ''}{pnl:.2f}")
        if reason:
            parts.append(reason)

        logger = logging.getLogger("fang_v10")
        logger.info(" | ".join(parts))

    def _on_bot_error(self, msg: str) -> None:
        self.lbl_bot_status.setText(f"오류: {msg}")
        logger = logging.getLogger("fang_v10")
        logger.error(msg)

    def _on_bot_log(self, msg: str) -> None:
        logger = logging.getLogger("fang_v10")
        logger.info(msg)

    def _save_params(self) -> None:
        cfg = {
            "RISK_PER_TRADE_PCT": self._spins["RISK_PER_TRADE_PCT"].value(),
            "SL_ATR_MULT": {
                "BTC": self._spins["SL_BTC"].value(),
                "ETH": self._spins["SL_ETH"].value(),
            },
            "DCA_ATR_MULT": self._spins["DCA_ATR_MULT"].value(),
            "TP1_R": self._spins["TP1_R"].value(),
            "TP1_RATIO": self._spins["TP1_RATIO"].value() / 100,
            "TP2_R": self._spins["TP2_R"].value(),
            "TP2_RATIO": self._spins["TP2_RATIO"].value() / 100,
            "TP3_TRAIL_R": self._spins["TP3_TRAIL_R"].value(),
            "BE_TRIGGER_R": self._spins["BE_TRIGGER_R"].value(),
            "COOLDOWN_NORMAL_SEC": self._spins["COOLDOWN_NORMAL_SEC"].value(),
            "COOLDOWN_AFTER_SL_SEC": self._spins["COOLDOWN_AFTER_SL_SEC"].value(),
            "COOLDOWN_AFTER_SL_SAME_DIR_SEC": self._spins["COOLDOWN_AFTER_SL_SAME_DIR_SEC"].value(),
            "DAILY_LOSS_SOFT": self._spins["DAILY_LOSS_SOFT"].value(),
            "DAILY_LOSS_HARD": self._spins["DAILY_LOSS_HARD"].value(),
            "DAILY_LOSS_STOP": self._spins["DAILY_LOSS_STOP"].value(),
            "MONTHLY_MDD_LIMIT": self._spins["MONTHLY_MDD_LIMIT"].value(),
        }
        config_path = Path(CONFIG.DATA_DIR) / "user_config.json"
        existing = {}
        if config_path.exists():
            try:
                existing = json.loads(config_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                pass
        existing.update(cfg)
        config_path.write_text(json.dumps(existing, indent=2, ensure_ascii=False), encoding="utf-8")
        CONFIG.sync_from_json()
        QMessageBox.information(self, "저장", "설정이 저장되었습니다. 즉시 반영됩니다.")

    # ──────────── 탭 6: 로그 ────────────
    def _build_tab6(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)

        # 필터 행
        filt = QHBoxLayout()
        self.log_level_cmb = QComboBox()
        self.log_level_cmb.addItems(["전체", "INFO", "WARNING", "ERROR", "CRITICAL"])
        self.log_level_cmb.currentIndexChanged.connect(self._refresh_log_view)
        self.log_sym_cmb = QComboBox()
        self.log_sym_cmb.addItems(["전체", "BTC", "ETH"])
        self.log_sym_cmb.currentIndexChanged.connect(self._refresh_log_view)

        btn_clear = QPushButton("\U0001f5d1 로그 지우기")
        btn_clear.clicked.connect(self._clear_log)

        btn_open = QPushButton("\U0001f4c1 로그 폴더 열기")
        btn_open.clicked.connect(self._open_log_folder)

        lbl_hint = QLabel("최근 500줄 표시")
        lbl_hint.setStyleSheet("color:#999;")

        filt.addWidget(QLabel("레벨:"))
        filt.addWidget(self.log_level_cmb)
        filt.addWidget(QLabel("심볼:"))
        filt.addWidget(self.log_sym_cmb)
        filt.addWidget(btn_clear)
        filt.addWidget(btn_open)
        filt.addStretch()
        filt.addWidget(lbl_hint)
        lay.addLayout(filt)

        # 로그 텍스트
        self.log_text = QTextEdit()
        self.log_text.setReadOnly(True)
        self.log_text.setFont(QFont("Consolas", 11))
        self.log_text.setStyleSheet(
            "background:#0a0a1a; color:#e0e0e0; border:none; padding:8px;"
        )
        lay.addWidget(self.log_text, stretch=1)
        return w

    def _setup_log_handler(self) -> None:
        handler = _QTextEditHandler(self.log_signal)
        handler.setLevel(logging.INFO)
        handler.setFormatter(logging.Formatter(
            "%(asctime)s | %(levelname)-8s | %(message)s",
            datefmt="%H:%M:%S",
        ))
        root = logging.getLogger("fang_v10")
        root.addHandler(handler)

    _LOG_LEVEL_ORDER = {"INFO": 0, "WARNING": 1, "ERROR": 2, "CRITICAL": 3}
    _LOG_COLORS = {
        "INFO": "#e0e0e0",
        "WARNING": "#ffc107",
        "ERROR": "#ff5252",
        "CRITICAL": "#ff1744",
    }

    def _append_log_line(self, line: str) -> None:
        self._log_lines.append(line)
        if len(self._log_lines) > 500:
            self._log_lines = self._log_lines[-500:]
        self._render_one_line(line)

    def _render_one_line(self, line: str) -> None:
        """필터 통과하면 텍스트에 HTML 한 줄 추가."""
        level_filter = self.log_level_cmb.currentText()
        sym_filter = self.log_sym_cmb.currentText()

        if not self._line_passes_filter(line, level_filter, sym_filter):
            return

        level = self._extract_level(line)
        color = self._LOG_COLORS.get(level, "#e0e0e0")
        bold = "font-weight:bold;" if level == "CRITICAL" else ""

        import html as _html
        escaped = _html.escape(line)
        self.log_text.append(
            f"<span style='color:{color};{bold}'>{escaped}</span>"
        )
        # 자동 스크롤
        sb = self.log_text.verticalScrollBar()
        sb.setValue(sb.maximum())

    @staticmethod
    def _extract_level(line: str) -> str:
        for lvl in ("CRITICAL", "ERROR", "WARNING", "INFO"):
            if lvl in line:
                return lvl
        return "INFO"

    def _line_passes_filter(self, line: str, level_filter: str, sym_filter: str) -> bool:
        if level_filter != "전체":
            line_level = self._extract_level(line)
            min_ord = self._LOG_LEVEL_ORDER.get(level_filter, 0)
            line_ord = self._LOG_LEVEL_ORDER.get(line_level, 0)
            if line_ord < min_ord:
                return False
        if sym_filter != "전체" and sym_filter not in line:
            return False
        return True

    def _refresh_log_view(self) -> None:
        """필터 변경 시 전체 다시 렌더링."""
        self.log_text.clear()
        level_filter = self.log_level_cmb.currentText()
        sym_filter = self.log_sym_cmb.currentText()

        import html as _html
        parts = []
        for line in self._log_lines:
            if not self._line_passes_filter(line, level_filter, sym_filter):
                continue
            level = self._extract_level(line)
            color = self._LOG_COLORS.get(level, "#e0e0e0")
            bold = "font-weight:bold;" if level == "CRITICAL" else ""
            escaped = _html.escape(line)
            parts.append(f"<span style='color:{color};{bold}'>{escaped}</span>")
        if parts:
            self.log_text.setHtml("<br>".join(parts))
            sb = self.log_text.verticalScrollBar()
            sb.setValue(sb.maximum())

    def _clear_log(self) -> None:
        self._log_lines.clear()
        self.log_text.clear()

    @staticmethod
    def _open_log_folder() -> None:
        log_dir = str(Path(CONFIG.DATA_DIR) / "logs")
        os.makedirs(log_dir, exist_ok=True)
        if sys.platform == "win32":
            subprocess.Popen(f'explorer "{log_dir}"')
        elif sys.platform == "darwin":
            subprocess.Popen(["open", log_dir])
        else:
            subprocess.Popen(["xdg-open", log_dir])

    # ── 카드 값 업데이트 ──
    @staticmethod
    def _set_card(frame: QFrame, value: str, color: str = C_TEXT) -> None:
        lbl = frame.findChild(QLabel, "card_val")
        if lbl:
            lbl.setText(value)
            lbl.setStyleSheet(f"color:{color}; font-size:18pt; font-weight:bold;")

    def show(self) -> None:
        self.setStyleSheet(DARK_STYLE)
        super().show()
