"""
로깅 & 모니터링 모듈.

파일+콘솔 로깅, trades_log.jsonl 레코드 구조, 상태 로그.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path
from typing import Any, Dict, List, Optional

from fang_v10.config import CONFIG

logger = logging.getLogger("fang_v10")


def setup_logging(log_dir: Optional[str] = None) -> None:
    """파일(logs/trading.log) + 콘솔 로깅 설정. 일별 rotate."""
    log_path = Path(log_dir or CONFIG.DATA_DIR) / "logs"
    log_path.mkdir(parents=True, exist_ok=True)

    root_logger = logging.getLogger("fang_v10")
    root_logger.setLevel(logging.DEBUG)

    # 기존 핸들러 제거 (중복 방지)
    root_logger.handlers.clear()

    # 파일 핸들러 — 일별 rotate
    file_handler = TimedRotatingFileHandler(
        filename=str(log_path / "trading.log"),
        when="midnight",
        interval=1,
        backupCount=30,
        encoding="utf-8",
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(logging.Formatter(
        "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    ))
    root_logger.addHandler(file_handler)

    # 에러 전용 핸들러 — ERROR/CRITICAL만, 일별 rotate
    error_handler = TimedRotatingFileHandler(
        filename=str(log_path / "errors.log"),
        when="midnight",
        interval=1,
        backupCount=30,
        encoding="utf-8",
    )
    error_handler.setLevel(logging.ERROR)
    error_handler.setFormatter(logging.Formatter(
        "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s\n%(exc_info)s"
        if False else "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    ))
    root_logger.addHandler(error_handler)

    # 콘솔 핸들러
    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(logging.Formatter(
        "%(asctime)s | %(levelname)-8s | %(message)s",
        datefmt="%H:%M:%S",
    ))
    root_logger.addHandler(console_handler)

    # 거래 전용 로거 초기화 (날짜별 파일)
    _setup_trade_logger(log_path)

    logger.info("로깅 설정 완료: %s", log_path)


_trade_log_dir: Optional[Path] = None


def _setup_trade_logger(log_path: Path) -> None:
    """거래 전용 탭구분 로그 파일 초기화 (날짜별)."""
    global _trade_log_dir
    _trade_log_dir = log_path


def log_trade_to_file(record: Dict) -> None:
    """거래 레코드를 날짜별 trades_YYYY-MM-DD.log에 탭구분으로 기록."""
    if _trade_log_dir is None:
        return
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    filepath = _trade_log_dir / f"trades_{today}.log"

    # 헤더가 없으면 추가
    if not filepath.exists():
        filepath.write_text(
            "시간\t심볼\t방향\t타입\t진입가\t청산가\tPnL\tR값\t사유\n",
            encoding="utf-8",
        )

    line = (
        f"{record.get('time', '')}\t"
        f"{record.get('symbol', '')}\t"
        f"{record.get('side', '')}\t"
        f"{record.get('type', '')}\t"
        f"{record.get('entry_price', 0)}\t"
        f"{record.get('exit_price', 0)}\t"
        f"{record.get('pnl_usd', 0)}\t"
        f"{record.get('r_value', 0)}\t"
        f"{record.get('reason_detail', record.get('reason', ''))}\n"
    )
    with open(filepath, "a", encoding="utf-8") as f:
        f.write(line)


# ──────────────────────────────────────────────
# trades_log.jsonl 레코드 생성
# ──────────────────────────────────────────────

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def log_entry(
    signal: Any, sizing: Any, regime: str,
    indicators: Optional[Dict] = None,
) -> Dict:
    """진입 로그 레코드."""
    record = {
        "time": _now_iso(),
        "symbol": getattr(signal, "symbol", ""),
        "side": getattr(signal, "side", ""),
        "type": "ENTRY",
        "entry_price": getattr(sizing, "entry_price", 0),
        "exit_price": None,
        "pnl_usd": 0,
        "r_value": 0,
        "reason": getattr(signal, "reason", ""),
        "reason_detail": getattr(signal, "reason", ""),
        "regime": regime,
        "strategy": getattr(signal, "strategy", ""),
        "indicators": indicators or {},
        "dca_info": None,
        "phantom_result": None,
        "blocked_result": None,
        "hold_bars": 0,
        "peak_r": 0,
        "equity_after": 0,
    }
    logger.info(
        "ENTRY | %s %s %s @ %.2f | %s",
        record["symbol"], record["side"], record["strategy"],
        record["entry_price"], record["reason"],
    )
    log_trade_to_file(record)
    return record


def log_exit(
    symbol: str, side: str, exit_price: float,
    reason: str, reason_detail: str,
    pnl: float, r_value: float,
    mfe: float, mae: float,
    hold_bars: int, equity: float,
    regime: str = "", strategy: str = "",
    indicators: Optional[Dict] = None,
) -> Dict:
    """청산 로그 레코드."""
    record = {
        "time": _now_iso(),
        "symbol": symbol,
        "side": side,
        "type": reason,
        "entry_price": 0,  # 호출자가 채움
        "exit_price": exit_price,
        "pnl_usd": round(pnl, 4),
        "r_value": round(r_value, 3),
        "reason": reason,
        "reason_detail": reason_detail,
        "regime": regime,
        "strategy": strategy,
        "indicators": indicators or {},
        "dca_info": None,
        "phantom_result": None,
        "blocked_result": None,
        "hold_bars": hold_bars,
        "peak_r": mfe,
        "equity_after": round(equity, 2),
    }
    emoji = "+" if pnl >= 0 else ""
    logger.info(
        "EXIT  | %s %s %s | %s%.2f USDT (%.2fR) | %s | %d봉",
        symbol, side, reason, emoji, pnl, r_value, reason_detail, hold_bars,
    )
    log_trade_to_file(record)
    return record


def log_dca(
    symbol: str, dca_price: float, dca_amount: float,
    new_avg: float, total_risk_r: float,
) -> Dict:
    """DCA 로그 레코드."""
    record = {
        "time": _now_iso(),
        "symbol": symbol,
        "side": "",
        "type": "DCA",
        "entry_price": dca_price,
        "exit_price": None,
        "pnl_usd": 0,
        "r_value": 0,
        "reason": "DCA",
        "reason_detail": f"DCA @ {dca_price:.2f}, new_avg={new_avg:.2f}",
        "regime": "",
        "strategy": "",
        "indicators": {},
        "dca_info": {
            "new_avg": round(new_avg, 4),
            "total_risk_r": round(total_risk_r, 3),
        },
        "phantom_result": None,
        "blocked_result": None,
        "hold_bars": 0,
        "peak_r": 0,
        "equity_after": 0,
    }
    logger.info(
        "DCA   | %s @ %.2f | new_avg=%.2f, risk=%.2fR",
        symbol, dca_price, new_avg, total_risk_r,
    )
    log_trade_to_file(record)
    return record


def log_kill_switch(
    reason: str, daily_pnl: float, pause_until: Optional[float] = None,
) -> None:
    """킬스위치 발동 로그."""
    pause_str = ""
    if pause_until:
        remaining = max(0, pause_until - time.time())
        pause_str = f", 재개까지 {remaining:.0f}초"
    logger.warning(
        "KILL  | %s | 일간PnL=%.2f USDT%s",
        reason, daily_pnl, pause_str,
    )


def log_state(
    balance: float,
    positions: Dict[str, Any],
    daily_pnl: float,
    regimes: Dict[str, str],
    risk_mode: Dict[str, Any],
) -> None:
    """5분 주기 상태 로그."""
    pos_summary = []
    for key, pos in positions.items():
        if hasattr(pos, "calc_r"):
            pos_summary.append(f"{key}")
        else:
            pos_summary.append(key)

    logger.info(
        "STATE | bal=%.2f | daily=%.2f | risk=%s | pos=[%s] | regime=%s",
        balance, daily_pnl,
        risk_mode.get("mode", "?"),
        ", ".join(pos_summary) if pos_summary else "없음",
        json.dumps(regimes, ensure_ascii=False),
    )


def log_phantom_stats(stats: Dict) -> None:
    """PhantomTracker 통계 로그."""
    if stats.get("total", 0) == 0:
        return
    logger.info(
        "PHANTOM | total=%d | 선점=%d(%.1f%%) | 정확=%d(%.1f%%) | avg_fav=%.2f%% avg_adv=%.2f%%",
        stats["total"],
        stats["premature_count"], stats["premature_pct"],
        stats["correct_count"], stats["correct_pct"],
        stats["avg_peak_favorable_pct"], stats["avg_peak_adverse_pct"],
    )


def log_blocked_stats(stats: Dict) -> None:
    """BlockedSignalTracker 통계 로그."""
    if stats.get("total", 0) == 0:
        return
    logger.info(
        "BLOCKED | total=%d | 정확차단=%d(%.1f%%) | 오차단=%d(%.1f%%)",
        stats["total"],
        stats["correct_block_count"], stats["correct_block_pct"],
        stats["wrong_block_count"], stats["wrong_block_pct"],
    )
