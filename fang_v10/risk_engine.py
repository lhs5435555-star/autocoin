"""
v11 리스크 엔진.

DD step-down 사이징, DCA 예산 분할, 상관 리스크 캡,
5단계 킬스위치 (Daily/Weekly/Monthly), EV 자동 계산.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Dict, Optional

from fang_v10.config import CONFIG

logger = logging.getLogger(__name__)


# ══════════════════════════════════════════
# EV 요약 출력
# ══════════════════════════════════════════

def print_ev_summary(account: float, leverage: float = 10.0) -> None:
    """봇 시작 시 EV 자동 계산."""
    base_risk = 0.0025
    sl_dist = 0.0040
    fee_R = 0.0012 / sl_dist
    slip_R = 0.0008 / sl_dist
    total_fee_R = fee_R + slip_R

    avg_W = 1.5 * 0.40 + 2.5 * 0.35 + 4.0 * 0.25  # = 2.475R

    logger.info("═══ EV 요약 (account=$%.0f, lev=%.0fx) ═══", account, leverage)
    for W in [0.35, 0.40, 0.44, 0.50, 0.55]:
        ev = W * avg_W - (1 - W) * 1.0 - total_fee_R
        R_dollar = account * base_risk
        monthly_50 = 50 * ev * R_dollar
        logger.info(
            "  승률 %2.0f%%: EV=%+.3fR | 월50건 $%+.2f",
            W * 100, ev, monthly_50,
        )

    be_wr = (1.0 + total_fee_R) / (avg_W + 1.0) * 100
    logger.info("  손익분기 승률: %.1f%%", be_wr)

    R_dollar = account * base_risk
    notional = R_dollar / sl_dist
    min_btc = 0.001 * 84000
    status = "✅" if notional >= min_btc else "⚠️ 최소주문미달"
    logger.info(
        "  BTC 1R=$%.2f | notional=$%.0f %s",
        R_dollar, notional, status,
    )


# ══════════════════════════════════════════
# RiskEngine
# ══════════════════════════════════════════

class RiskEngine:
    """v11 리스크 엔진 — DD step-down + 5단계 킬스위치."""

    BASE_RISK = {
        "BTC": 0.0025,    # 0.25%
        "ETH": 0.0020,    # 0.20%
        "DEFAULT": 0.0025,
    }

    # DCA 예산 분할
    DCA_ENTRY_BUDGET_RATIO = 0.70
    DCA_RESERVE_RATIO = 0.30

    # 수수료/슬리피지
    FEE_PCT = 0.0012      # 왕복 0.12%
    SLIP_PCT = 0.0008     # 왕복 0.08%

    # 킬스위치 기준
    DAILY_SOFT = -0.010
    DAILY_HARD = -0.015
    DAILY_STOP = -0.020
    WEEKLY_STOP = -0.040
    MONTHLY_STOP = -0.050

    COIN_PAUSE_SEC: float = 7200.0
    GLOBAL_PAUSE_SEC: float = 14400.0

    def __init__(self) -> None:
        self._initial_balance: float = 0.0
        self._peak_equity: float = 0.0

        # PnL 추적 (비율)
        self.daily_pnl: float = 0.0
        self.weekly_pnl: float = 0.0
        self.monthly_pnl: float = 0.0
        self._daily_pnl: float = 0.0  # v10 호환 (USDT)
        self._daily_peak_pnl: float = 0.0
        self._last_reset_date: Optional[str] = None

        # 연속패
        self.consecutive_losses: int = 0
        self._coin_consec: Dict[str, int] = {}
        self._coin_paused: Dict[str, float] = {}
        self._global_consec: int = 0
        self._global_paused_until: float = 0.0

        # 킬스위치 상태
        self._halt_level: str = ""  # "", "SOFT", "HARD", "STOP", "WEEKLY_STOP", "MONTHLY_STOP"
        self._trading_stopped: bool = False
        self._halt_until: float = 0.0

    def set_initial_balance(self, balance: float) -> None:
        """초기 잔고 설정."""
        self._initial_balance = balance
        if self._peak_equity == 0:
            self._peak_equity = balance
        # 일간 리셋 (날짜 설정 포함)
        self._check_daily_reset()
        if self._last_reset_date is None:
            self._reset_daily()
            now = datetime.now(timezone.utc)
            hour = CONFIG.DAILY_RESET_HOUR_UTC
            if now.hour >= hour:
                self._last_reset_date = now.strftime("%Y-%m-%d")
            else:
                self._last_reset_date = (now.replace(hour=0) - __import__("datetime").timedelta(days=1)).strftime("%Y-%m-%d")

    # ──────────────────────────────────────
    # DD step-down 사이징
    # ──────────────────────────────────────

    def get_risk_pct(self, symbol: str) -> float:
        """DD 기반 리스크 비율 반환."""
        asset = "BTC" if "BTC" in symbol else ("ETH" if "ETH" in symbol else "DEFAULT")
        base = self.BASE_RISK.get(asset, self.BASE_RISK["DEFAULT"])

        if self._peak_equity <= 0:
            return base

        current = self._initial_balance + self._daily_pnl
        dd = (self._peak_equity - current) / self._peak_equity

        if dd < 0.03:
            return base
        elif dd < 0.05:
            return base * 0.75
        elif dd < 0.07:
            return base * 0.50
        elif dd >= 0.085:
            return 0.0
        else:
            return base * 0.25

    def calculate_position_size(
        self, symbol: str, entry: float, sl_price: float,
        leverage: float, account_balance: float,
    ) -> Dict:
        """v11 포지션 사이징."""
        risk_pct = self.get_risk_pct(symbol)
        R_dollar = account_balance * risk_pct

        sl_dist_pct = abs(entry - sl_price) / entry
        adj_sl = sl_dist_pct + self.FEE_PCT + self.SLIP_PCT

        qty_notional = R_dollar / adj_sl if adj_sl > 0 else 0
        margin_req = qty_notional / leverage if leverage > 0 else 0

        # 검증 게이트
        if leverage > 15.0:
            raise ValueError(f"LEVERAGE_EXCEEDED: {leverage} > 15")
        if margin_req > account_balance * 0.30:
            raise ValueError(f"MARGIN_TOO_LARGE: {margin_req:.2f} > {account_balance * 0.30:.2f}")
        if sl_dist_pct < 0.0015:
            raise ValueError(f"SL_TOO_TIGHT: {sl_dist_pct:.4%}")
        if sl_dist_pct > 0.020:
            raise ValueError(f"SL_TOO_WIDE: {sl_dist_pct:.4%}")

        fee_R = (self.FEE_PCT + self.SLIP_PCT) / sl_dist_pct if sl_dist_pct > 0 else 999

        return {
            "risk_pct": risk_pct,
            "R_dollar": round(R_dollar, 4),
            "qty_notional": round(qty_notional, 2),
            "margin_req": round(margin_req, 2),
            "sl_dist_pct": round(sl_dist_pct, 6),
            "fee_R": round(fee_R, 4),
        }

    # ──────────────────────────────────────
    # DCA 검증
    # ──────────────────────────────────────

    def validate_dca(
        self, symbol: str, account: float,
        entry_initial: float, qty_initial: float,
        dca_price: float, sl_price: float,
        dca_count: int, regime: str = "",
        plus_di: float = 0, minus_di: float = 0,
        oi_bullish: bool = True, side: str = "long",
    ) -> float:
        """DCA 허용 수량 반환. 0이면 DCA 거부."""
        if dca_count >= 1:
            return 0.0
        if regime in ("BOX", "NO_TRADE"):
            return 0.0
        if side == "long" and minus_di > plus_di:
            return 0.0
        if side == "short" and plus_di > minus_di:
            return 0.0
        if not oi_bullish:
            return 0.0

        # 미실현 R 확인
        r_dist = abs(entry_initial - sl_price)
        if r_dist <= 0:
            return 0.0
        unrealized_R = (dca_price - entry_initial) / r_dist if side == "long" else (entry_initial - dca_price) / r_dist
        if not (-0.75 <= unrealized_R <= -0.35):
            return 0.0

        risk_pct = self.get_risk_pct(symbol)
        R_dollar = account * risk_pct
        dca_budget = R_dollar * self.DCA_RESERVE_RATIO
        existing_risk = qty_initial * r_dist
        remaining = dca_budget - existing_risk
        if remaining <= 0:
            return 0.0

        dca_sl_dist = abs(dca_price - sl_price)
        if dca_sl_dist <= 0:
            return 0.0
        dca_qty = remaining / dca_sl_dist
        return min(dca_qty, qty_initial * 0.50)

    # ──────────────────────────────────────
    # PnL 기록 + 킬스위치
    # ──────────────────────────────────────

    def update_pnl(self, pnl_dollar: float, account: float, symbol: str = "") -> None:
        """거래 결과 기록 + 킬스위치 업데이트."""
        if account <= 0:
            return
        pnl_pct = pnl_dollar / account
        self.daily_pnl += pnl_pct
        self.weekly_pnl += pnl_pct
        self.monthly_pnl += pnl_pct

        # v10 호환
        self._daily_pnl += pnl_dollar
        if self._daily_pnl > self._daily_peak_pnl:
            self._daily_peak_pnl = self._daily_pnl

        # 피크 갱신
        current = account + pnl_dollar
        if current > self._peak_equity:
            self._peak_equity = current

        # 연속패
        if pnl_dollar < 0:
            self.consecutive_losses += 1
            self._coin_consec[symbol] = self._coin_consec.get(symbol, 0) + 1
            self._global_consec += 1

            if self._coin_consec.get(symbol, 0) >= CONFIG.CONSEC_LOSS_LIMIT:
                self._coin_paused[symbol] = time.time() + self.COIN_PAUSE_SEC
            if self._global_consec >= 5:
                self._global_paused_until = time.time() + self.GLOBAL_PAUSE_SEC

            if self.consecutive_losses >= 5:
                logger.warning("연속패 %d회 — SOFT 경고", self.consecutive_losses)
        else:
            self.consecutive_losses = 0
            self._coin_consec[symbol] = 0
            self._global_consec = 0

        # 킬스위치 판정
        self._check_kill_levels()

    def record_trade(self, pnl: float, symbol: str) -> None:
        """v10 호환: update_pnl 호출."""
        self.update_pnl(pnl, self._initial_balance, symbol)

    def _check_kill_levels(self) -> None:
        """킬스위치 레벨 판정."""
        if self.monthly_pnl <= self.MONTHLY_STOP:
            self._halt_level = "MONTHLY_STOP"
            self._trading_stopped = True
            logger.critical("MONTHLY_STOP: 월간 PnL %.2f%%", self.monthly_pnl * 100)
        elif self.weekly_pnl <= self.WEEKLY_STOP:
            self._halt_level = "WEEKLY_STOP"
            self._halt_until = time.time() + 48 * 3600
            self._trading_stopped = True
            logger.critical("WEEKLY_STOP: 주간 PnL %.2f%% → 48시간 정지", self.weekly_pnl * 100)
        elif self.daily_pnl <= self.DAILY_STOP:
            self._halt_level = "STOP"
            self._halt_until = time.time() + 24 * 3600
            self._trading_stopped = True
            logger.critical("DAILY_STOP: 일간 PnL %.2f%% → 24시간 정지", self.daily_pnl * 100)
        elif self.daily_pnl <= self.DAILY_HARD:
            self._halt_level = "HARD"
            logger.warning("DAILY_HARD: 일간 PnL %.2f%% → 전체 청산", self.daily_pnl * 100)
        elif self.daily_pnl <= self.DAILY_SOFT:
            self._halt_level = "SOFT"
            logger.info("DAILY_SOFT: 일간 PnL %.2f%% → 신규 진입 차단", self.daily_pnl * 100)

    def check_entry_allowed(self) -> tuple:
        """진입 가능 여부."""
        self._check_daily_reset()

        if self._trading_stopped:
            if self._halt_until > 0 and time.time() > self._halt_until:
                self._trading_stopped = False
                self._halt_level = ""
                self._halt_until = 0
            else:
                return False, f"HALT:{self._halt_level}"

        if self._halt_level in ("SOFT", "HARD", "STOP",
                                "WEEKLY_STOP", "MONTHLY_STOP"):
            return False, f"HALT:{self._halt_level}"

        if time.time() < self._global_paused_until:
            return False, "GLOBAL_CONSEC_PAUSE"

        if self.get_risk_pct("BTC") == 0:
            return False, "DD_HALT"

        return True, "OK"

    # ──────────────────────────────────────
    # BTC+ETH 상관 리스크 캡
    # ──────────────────────────────────────

    def check_correlation_cap(
        self, account: float,
        btc_open_risk: float, eth_open_risk: float,
        new_risk: float,
    ) -> bool:
        """동일방향 동시 보유 리스크 캡: 0.55%."""
        combined = btc_open_risk + eth_open_risk + new_risk
        cap = account * 0.0055
        if combined > cap:
            logger.info(
                "상관 리스크 캡 초과: combined=$%.2f > cap=$%.2f",
                combined, cap,
            )
            return False
        return True

    # ──────────────────────────────────────
    # v10 호환 인터페이스
    # ──────────────────────────────────────

    def can_trade(self) -> bool:
        allowed, _ = self.check_entry_allowed()
        return allowed

    def can_trade_coin(self, symbol: str) -> tuple:
        if not self.can_trade():
            return False, "전체 매매 정지"
        paused = self._coin_paused.get(symbol, 0)
        if time.time() < paused:
            return False, f"{symbol} 연패 정지"
        return True, "OK"

    def get_risk_mode(self) -> Dict:
        self._check_daily_reset()
        mode = "NORMAL"
        size_mult = 1.0
        can_enter = True
        reason = "정상"

        if self._halt_level == "STOP" or self._trading_stopped:
            mode, size_mult, can_enter = "STOP", 0.0, False
            reason = f"일간 PnL {self.daily_pnl * 100:.2f}%"
        elif self._halt_level == "HARD":
            mode, size_mult, can_enter = "HARD", 0.0, False
            reason = f"일간 PnL {self.daily_pnl * 100:.2f}%"
        elif self._halt_level == "SOFT":
            mode, size_mult, can_enter = "SOFT", 0.5, False
            reason = f"일간 PnL {self.daily_pnl * 100:.2f}%"

        return {"mode": mode, "size_mult": size_mult,
                "can_enter": can_enter, "reason": reason}

    def get_state(self) -> Dict:
        mode = self.get_risk_mode()
        mode["daily_pnl"] = self._daily_pnl
        mode["consecutive_losses"] = self._global_consec
        mode["trading_stopped"] = self._trading_stopped
        return mode

    def _check_daily_reset(self) -> None:
        now = datetime.now(timezone.utc)
        hour = CONFIG.DAILY_RESET_HOUR_UTC
        if now.hour >= hour:
            reset_date = now.strftime("%Y-%m-%d")
        else:
            reset_date = (now.replace(hour=0) - __import__("datetime").timedelta(days=1)).strftime("%Y-%m-%d")
        if self._last_reset_date != reset_date:
            self._reset_daily()
            self._last_reset_date = reset_date

    def _reset_daily(self) -> None:
        self.daily_pnl = 0.0
        self._daily_pnl = 0.0
        self._daily_peak_pnl = 0.0
        self._halt_level = ""
        self._trading_stopped = False
        self._halt_until = 0.0
        self.consecutive_losses = 0
        self._coin_consec.clear()
        self._coin_paused.clear()
        self._global_consec = 0
        self._global_paused_until = 0.0
        logger.info("일간 리스크 리셋 완료")


# ══════════════════════════════════════════
# MddTracker (v10 호환 유지)
# ══════════════════════════════════════════

class MddTracker:
    """주간/월간 MDD 추적."""

    def __init__(
        self,
        weekly_limit: float = CONFIG.WEEKLY_MDD_LIMIT,
        monthly_limit: float = CONFIG.MONTHLY_MDD_LIMIT,
    ) -> None:
        self.weekly_limit = weekly_limit
        self.monthly_limit = monthly_limit
        self._weekly_peak: float = 0.0
        self._monthly_peak: float = 0.0
        self._weekly_dd: float = 0.0
        self._monthly_dd: float = 0.0
        self._monthly_stopped: bool = False
        self._last_week: Optional[int] = None
        self._last_month: Optional[int] = None

    def update(self, balance: float, trade_pnl: float = 0.0) -> Dict:
        self._check_period_reset(balance)
        if self._monthly_stopped:
            return {"allowed": False, "reason": "월간 MDD 정지",
                    "weekly_dd": self._weekly_dd, "monthly_dd": self._monthly_dd}

        if balance > self._weekly_peak:
            self._weekly_peak = balance
        if balance > self._monthly_peak:
            self._monthly_peak = balance

        if self._weekly_peak > 0:
            self._weekly_dd = (self._weekly_peak - balance) / self._weekly_peak * 100
        if self._monthly_peak > 0:
            self._monthly_dd = (self._monthly_peak - balance) / self._monthly_peak * 100

        if self._monthly_dd >= self.monthly_limit:
            self._monthly_stopped = True
            return {"allowed": False, "reason": f"월간 MDD {self._monthly_dd:.2f}%",
                    "weekly_dd": self._weekly_dd, "monthly_dd": self._monthly_dd}

        return {"allowed": True, "reason": "정상",
                "weekly_dd": round(self._weekly_dd, 4),
                "monthly_dd": round(self._monthly_dd, 4)}

    def get_state(self) -> Dict:
        return {
            "weekly_peak": self._weekly_peak,
            "monthly_peak": self._monthly_peak,
            "weekly_dd": self._weekly_dd,
            "monthly_dd": self._monthly_dd,
            "monthly_stopped": self._monthly_stopped,
        }

    def reset_monthly_stop(self, balance: float) -> None:
        self._monthly_stopped = False
        self._monthly_peak = balance
        self._monthly_dd = 0.0

    def _check_period_reset(self, balance: float) -> None:
        now = datetime.now(timezone.utc)
        current_week = now.isocalendar()[1]
        current_month = now.month

        if self._last_week is not None and current_week != self._last_week:
            self._weekly_peak = balance
            self._weekly_dd = 0.0
        self._last_week = current_week

        if self._last_month is not None and current_month != self._last_month:
            self._monthly_peak = balance
            self._monthly_dd = 0.0
            self._monthly_stopped = False
        self._last_month = current_month
