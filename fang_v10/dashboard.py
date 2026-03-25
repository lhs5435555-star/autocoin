"""
PyQt5 데스크톱 대시보드 — FANG SCALPER v10.

탭 3개: 현황 / 거래내역 / 분석.
봇 로직 없음. trades_log.jsonl + state.json 읽기 전용.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Dict, List

from PyQt5.QtCore import Qt, QTimer
from PyQt5.QtGui import QColor, QFont
from PyQt5.QtWidgets import (
    QApplication, QComboBox, QFrame, QGroupBox, QHBoxLayout, QHeaderView,
    QLabel, QMainWindow, QTabWidget, QTableWidget, QTableWidgetItem,
    QVBoxLayout, QWidget,
)

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

class Dashboard(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("FANG SCALPER v10")
        self.setMinimumSize(1400, 900)

        tabs = QTabWidget()
        self.setCentralWidget(tabs)
        tabs.addTab(self._build_tab1(), "📊 현황")
        tabs.addTab(self._build_tab2(), "📋 거래내역")
        tabs.addTab(self._build_tab3(), "📈 분석")

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
        self.c_bal = _card("💰 잔고", "$—")
        self.c_pnl = _card("📈 오늘 PnL", "$—")
        self.c_cum = _card("📊 누적 수익률", "—%")
        self.c_risk = _card("🛡 리스크", "—")
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

        # 하단 상태바
        regime_parts = []
        for sym in CONFIG.SYMBOLS:
            asset = "BTC" if "BTC" in sym else "ETH"
            regime_parts.append(f"{asset}: —")
        self.lbl_regime.setText("  |  ".join(regime_parts))

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
