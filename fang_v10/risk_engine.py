"""
리스크 엔진.

일간 3단계 킬스위치, 심볼/전체 연속패, 수익 보호, MDD 추적.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Dict, Optional

from fang_v10.config import CONFIG

logger = logging.getLogger(__name__)


class RiskEngine:
    """일간 리스크 관리.

    킬스위치 3단계 (daily_loss 기준, CONFIG.DAILY_RESET_HOUR_UTC 리셋):
      SOFT  (>=1.0%): size_mult=0.5, can_enter=True
      HARD  (>=1.5%): size_mult=0, can_enter=False
      STOP  (>=2.0%): trading_paused=True

    심볼별 연속패: 3연패 → 해당 심볼 2시간 정지
    전체 연속패:   5연패 → 전체 4시간 정지

    수익 보호:
      +3% → CAUTION (size_mult=0.5)
      +5% → PROTECT (size_mult=0.2)
      피크 대비 50% 되돌림 → 오늘 매매 중단
    """

    COIN_PAUSE_SEC: float = 7200.0    # 2시간
    GLOBAL_PAUSE_SEC: float = 14400.0  # 4시간

    def __init__(self) -> None:
        self._initial_balance: float = 0.0
        self._daily_pnl: float = 0.0
        self._daily_peak_pnl: float = 0.0
        self._last_reset_date: Optional[str] = None

        # 연속패 추적
        self._coin_consec: Dict[str, int] = {}     # 심볼별 연패 카운트
        self._coin_paused: Dict[str, float] = {}   # 심볼별 정지 해제 시각
        self._global_consec: int = 0                 # 전체 연패 카운트
        self._global_paused_until: float = 0.0       # 전체 정지 해제 시각

        self._trading_stopped: bool = False          # STOP 레벨

    def set_initial_balance(self, balance: float) -> None:
        """일간 기준 잔고 설정."""
        self._initial_balance = balance
        self._reset_daily()

    def record_trade(self, pnl: float, symbol: str) -> None:
        """거래 결과 기록 + 자동 킬스위치 체크.

        Args:
            pnl: 실현 손익 (USDT, 음수=손실)
            symbol: 심볼
        """
        self._check_daily_reset()
        self._daily_pnl += pnl

        # 일간 피크 갱신
        if self._daily_pnl > self._daily_peak_pnl:
            self._daily_peak_pnl = self._daily_pnl

        # ── 연속패 관리 ──
        if pnl < 0:
            # 심볼별
            self._coin_consec[symbol] = self._coin_consec.get(symbol, 0) + 1
            if self._coin_consec[symbol] >= CONFIG.CONSEC_LOSS_LIMIT:
                pause_until = time.time() + self.COIN_PAUSE_SEC
                self._coin_paused[symbol] = pause_until
                logger.warning(
                    "%s %d연패 → %.0f초 정지",
                    symbol, self._coin_consec[symbol], self.COIN_PAUSE_SEC,
                )

            # 전체
            self._global_consec += 1
            if self._global_consec >= 5:
                self._global_paused_until = time.time() + self.GLOBAL_PAUSE_SEC
                logger.warning(
                    "전체 %d연패 → %.0f초 정지",
                    self._global_consec, self.GLOBAL_PAUSE_SEC,
                )
        else:
            # 수익 거래 시 연패 리셋
            self._coin_consec[symbol] = 0
            self._global_consec = 0

        # ── 킬스위치 STOP 체크 ──
        if self._initial_balance > 0:
            daily_loss_pct = -self._daily_pnl / self._initial_balance * 100
            if daily_loss_pct >= CONFIG.DAILY_LOSS_STOP:
                self._trading_stopped = True
                logger.critical(
                    "STOP 레벨 도달: 일간 손실 %.2f%% → 매매 중단",
                    daily_loss_pct,
                )

    def get_risk_mode(self) -> Dict:
        """현재 리스크 모드 반환.

        Returns:
            {"mode": str, "size_mult": float, "can_enter": bool, "reason": str}
        """
        self._check_daily_reset()

        if self._initial_balance <= 0:
            return {"mode": "NORMAL", "size_mult": 1.0,
                    "can_enter": True, "reason": "잔고 미설정"}

        daily_loss_pct = -self._daily_pnl / self._initial_balance * 100
        daily_gain_pct = self._daily_pnl / self._initial_balance * 100

        # ── 킬스위치 (손실 기반) ──
        if self._trading_stopped or daily_loss_pct >= CONFIG.DAILY_LOSS_STOP:
            return {"mode": "STOP", "size_mult": 0.0,
                    "can_enter": False, "reason": f"일간 손실 {daily_loss_pct:.2f}% >= STOP"}

        if daily_loss_pct >= CONFIG.DAILY_LOSS_HARD:
            return {"mode": "HARD", "size_mult": 0.0,
                    "can_enter": False, "reason": f"일간 손실 {daily_loss_pct:.2f}% >= HARD"}

        if daily_loss_pct >= CONFIG.DAILY_LOSS_SOFT:
            return {"mode": "SOFT", "size_mult": 0.5,
                    "can_enter": True, "reason": f"일간 손실 {daily_loss_pct:.2f}% >= SOFT"}

        # ── 수익 보호 ──
        # 피크 대비 50% 되돌림 → 매매 중단
        if self._daily_peak_pnl > 0 and self._daily_pnl > 0:
            retracement = 1.0 - (self._daily_pnl / self._daily_peak_pnl)
            if retracement >= 0.5:
                return {"mode": "PROFIT_STOP", "size_mult": 0.0,
                        "can_enter": False,
                        "reason": f"수익 피크 대비 {retracement:.0%} 되돌림"}

        if daily_gain_pct >= 5.0:
            return {"mode": "PROFIT_PROTECT", "size_mult": 0.2,
                    "can_enter": True, "reason": f"일간 수익 {daily_gain_pct:.2f}% >= 5%"}

        if daily_gain_pct >= 3.0:
            return {"mode": "PROFIT_CAUTION", "size_mult": 0.5,
                    "can_enter": True, "reason": f"일간 수익 {daily_gain_pct:.2f}% >= 3%"}

        return {"mode": "NORMAL", "size_mult": 1.0,
                "can_enter": True, "reason": "정상"}

    def can_trade(self) -> bool:
        """전체 매매 가능 여부."""
        self._check_daily_reset()

        if self._trading_stopped:
            return False

        # 전체 연패 정지
        if time.time() < self._global_paused_until:
            return False

        mode = self.get_risk_mode()
        return mode["can_enter"]

    def can_trade_coin(self, symbol: str) -> tuple[bool, str]:
        """심볼별 매매 가능 여부.

        Returns:
            (allowed, reason)
        """
        if not self.can_trade():
            return False, "전체 매매 정지 중"

        paused_until = self._coin_paused.get(symbol, 0)
        if time.time() < paused_until:
            remaining = paused_until - time.time()
            return False, f"{symbol} {self._coin_consec.get(symbol, 0)}연패 정지 (잔여 {remaining:.0f}초)"

        return True, "정상"

    def _check_daily_reset(self) -> None:
        """UTC DAILY_RESET_HOUR_UTC 기준 일간 리셋 체크."""
        now = datetime.now(timezone.utc)
        hour = CONFIG.DAILY_RESET_HOUR_UTC

        # 리셋 시각 이후의 날짜 문자열
        if now.hour >= hour:
            reset_date = now.strftime("%Y-%m-%d")
        else:
            reset_date = (now.replace(hour=0) - __import__("datetime").timedelta(days=1)).strftime("%Y-%m-%d")

        if self._last_reset_date != reset_date:
            self._reset_daily()
            self._last_reset_date = reset_date

    def _reset_daily(self) -> None:
        """일간 데이터 리셋."""
        self._daily_pnl = 0.0
        self._daily_peak_pnl = 0.0
        self._trading_stopped = False
        self._coin_consec.clear()
        self._coin_paused.clear()
        self._global_consec = 0
        self._global_paused_until = 0.0
        logger.info("일간 리스크 리셋 완료")


class MddTracker:
    """주간/월간 MDD 추적.

    weekly_limit=3.0%, monthly_limit=5.0%
    월간 MDD -5% 도달 시 수동 재개만 허용.
    """

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
        """잔고/거래 결과로 MDD 업데이트.

        Args:
            balance: 현재 잔고 (USDT)
            trade_pnl: 이번 거래 PnL (USDT), 없으면 0

        Returns:
            {"allowed": bool, "reason": str,
             "weekly_dd": float, "monthly_dd": float}
        """
        self._check_period_reset(balance)

        if self._monthly_stopped:
            return {
                "allowed": False,
                "reason": f"월간 MDD {self._monthly_dd:.2f}% >= {self.monthly_limit}% (수동 재개 필요)",
                "weekly_dd": self._weekly_dd,
                "monthly_dd": self._monthly_dd,
            }

        # 피크 갱신
        if balance > self._weekly_peak:
            self._weekly_peak = balance
        if balance > self._monthly_peak:
            self._monthly_peak = balance

        # DD 계산
        if self._weekly_peak > 0:
            self._weekly_dd = (self._weekly_peak - balance) / self._weekly_peak * 100
        if self._monthly_peak > 0:
            self._monthly_dd = (self._monthly_peak - balance) / self._monthly_peak * 100

        # 월간 한도 체크
        if self._monthly_dd >= self.monthly_limit:
            self._monthly_stopped = True
            logger.critical(
                "월간 MDD %.2f%% >= %.2f%% → 매매 중단 (수동 재개만 허용)",
                self._monthly_dd, self.monthly_limit,
            )
            return {
                "allowed": False,
                "reason": f"월간 MDD {self._monthly_dd:.2f}% 한도 초과",
                "weekly_dd": self._weekly_dd,
                "monthly_dd": self._monthly_dd,
            }

        # 주간 한도 경고 (매매 허용하되 경고)
        reason = "정상"
        if self._weekly_dd >= self.weekly_limit:
            reason = f"주간 MDD {self._weekly_dd:.2f}% >= {self.weekly_limit}% (주의)"
            logger.warning(reason)

        return {
            "allowed": True,
            "reason": reason,
            "weekly_dd": round(self._weekly_dd, 4),
            "monthly_dd": round(self._monthly_dd, 4),
        }

    def reset_monthly_stop(self, balance: float) -> None:
        """수동 재개: 월간 MDD 정지 해제."""
        self._monthly_stopped = False
        self._monthly_peak = balance
        self._monthly_dd = 0.0
        logger.info("월간 MDD 정지 수동 해제, 피크 재설정: %.2f", balance)

    def _check_period_reset(self, balance: float) -> None:
        """주/월 경계에서 피크 리셋."""
        now = datetime.now(timezone.utc)
        current_week = now.isocalendar()[1]
        current_month = now.month

        if self._last_week is not None and current_week != self._last_week:
            self._weekly_peak = balance
            self._weekly_dd = 0.0
            logger.info("주간 MDD 리셋, 새 피크: %.2f", balance)
        self._last_week = current_week

        if self._last_month is not None and current_month != self._last_month:
            self._monthly_peak = balance
            self._monthly_dd = 0.0
            self._monthly_stopped = False
            logger.info("월간 MDD 리셋, 새 피크: %.2f", balance)
        self._last_month = current_month
