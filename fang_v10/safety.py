"""
v11 안전장치 모듈.

서버 SL 복원, 주문 Idempotency, State Reconcile, Mark Price 감시.
"""

from __future__ import annotations

import hashlib
import logging
import time
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


# ══════════════════════════════════════════
# 9-A. 서버사이드 SL 발행 (봇 크래시 대비)
# ══════════════════════════════════════════

def place_server_sl(
    symbol: str, sl_price: float, qty: float,
    client: Any, side: str = "long", max_retry: int = 3,
) -> Optional[Dict]:
    """거래소 서버에 Stop-Market SL 발행. 봇 꺼져도 자동 청산.

    실패 시 지수 백오프 재시도 → 최종 실패 시 CRITICAL.
    """
    for attempt in range(max_retry):
        try:
            result = client.set_trigger_sl(
                symbol, side, sl_price, qty,
            )
            logger.info(
                "[SERVER_SL] %s %s SL=%.2f qty=%.6f 설정 완료",
                symbol, side, sl_price, qty,
            )
            return result
        except Exception as e:
            wait = 2 ** attempt
            logger.warning(
                "[SERVER_SL] %s 실패 (%d/%d): %s → %ds 대기",
                symbol, attempt + 1, max_retry, e, wait,
            )
            time.sleep(wait)

    logger.critical("[SERVER_SL_FAILED] %s — SL 설정 불가", symbol)
    return None


def on_entry_filled(
    symbol: str, qty: float, fill_price: float,
    sl_price: float, side: str, client: Any,
) -> bool:
    """진입 체결 후 즉시 서버 SL 설정. 실패 시 포지션 청산."""
    order = place_server_sl(symbol, sl_price, qty, client, side)
    if order is None:
        logger.critical(
            "[SAFETY] %s SERVER_SL 실패 → 포지션 즉시 청산", symbol,
        )
        try:
            close_side = "sell" if side == "long" else "buy"
            client.create_market_order(symbol, close_side, qty, reduce_only=True)
        except Exception as e:
            logger.critical("[SAFETY] 비상 청산도 실패: %s — %s", symbol, e)
        return False
    return True


# ══════════════════════════════════════════
# 9-B. 주문 Idempotency (중복 방지)
# ══════════════════════════════════════════

def make_client_oid(
    symbol: str, side: str, purpose: str, ts_sec: int = 0,
) -> str:
    """고유 주문 ID 생성.

    형식: "{SYM3}_{S}_{PURPOSE}_{TS}_{HASH4}"
    """
    if ts_sec == 0:
        ts_sec = int(time.time())

    sym3 = symbol.replace("/USDT:USDT", "")[:3]
    s = "L" if side == "long" else "S"
    raw = f"{sym3}_{s}_{purpose}_{ts_sec}"
    h = hashlib.md5(raw.encode()).hexdigest()[:4]
    return f"{sym3}_{s}_{purpose}_{ts_sec}_{h}"


class OrderStore:
    """발행된 주문 ID 추적 (메모리 + 파일)."""

    def __init__(self):
        self._orders: Dict[str, Dict] = {}  # client_oid → order info

    def exists(self, client_oid: str) -> bool:
        return client_oid in self._orders

    def get(self, client_oid: str) -> Optional[Dict]:
        return self._orders.get(client_oid)

    def save(self, client_oid: str, order: Dict) -> None:
        self._orders[client_oid] = {
            "order": order,
            "ts": time.time(),
            "status": order.get("status", "open"),
        }

    def cleanup_old(self, max_age_sec: int = 86400) -> None:
        """24시간 이상 오래된 주문 정리."""
        now = time.time()
        expired = [k for k, v in self._orders.items()
                   if now - v["ts"] > max_age_sec]
        for k in expired:
            del self._orders[k]


def place_order_idempotent(
    symbol: str, side: str, qty: float,
    order_type: str, client_oid: str,
    reduce_only: bool, client: Any,
    order_store: OrderStore,
    price: float = 0,
) -> Optional[Dict]:
    """Idempotent 주문 발행.

    이미 발행된 client_oid면 재발행 금지.
    """
    # 1. 기존 주문 확인
    if order_store.exists(client_oid):
        existing = order_store.get(client_oid)
        logger.info("[IDEMPOTENT] %s 이미 발행됨: %s", symbol, client_oid)
        return existing.get("order")

    # 2. 신규 발행
    order_side = "buy" if side == "long" else "sell"
    if reduce_only:
        order_side = "sell" if side == "long" else "buy"

    for attempt in range(3):
        try:
            if order_type == "market":
                order = client.create_market_order(
                    symbol, order_side, qty, reduce_only=reduce_only,
                )
            else:
                order = client.create_market_order(
                    symbol, order_side, qty, reduce_only=reduce_only,
                )
            order_store.save(client_oid, order)
            logger.info("[ORDER] %s %s qty=%.6f oid=%s", symbol, order_type, qty, client_oid)
            return order
        except Exception as e:
            wait = 2 ** attempt
            logger.warning("[ORDER] %s 실패 (%d/3): %s → %ds", symbol, attempt + 1, e, wait)
            time.sleep(wait)

    logger.critical("[ORDER_FAILED] %s — 3회 실패: %s", symbol, client_oid)
    return None


# ══════════════════════════════════════════
# 9-C. 재시작 State Reconciliation
# ══════════════════════════════════════════

def reconcile_on_startup(
    state_store: Any, client: Any,
    risk_engine: Any, pos_mgr: Any,
    symbols: List[str],
) -> Dict:
    """봇 시작 시 로컬-거래소 상태 동기화."""
    details = []
    status = "OK"

    # Step 1: 로컬 포지션
    local_keys = set(pos_mgr.positions.keys())

    # Step 2: 거래소 포지션
    exchange_positions = {}
    for sym in symbols:
        try:
            pos = client.get_position(sym)
            if pos and float(pos.get("contracts", 0) or 0) > 0:
                side = pos.get("side", "long")
                key = f"{sym}|{side}"
                exchange_positions[key] = pos
        except Exception as e:
            logger.warning("[RECONCILE] %s 조회 실패: %s", sym, e)

    exchange_keys = set(exchange_positions.keys())

    # Step 3: 비교
    # 로컬에 있고 거래소에 없음 → 이미 청산됨
    for key in local_keys - exchange_keys:
        status = "MISMATCH"
        details.append(f"CLOSED_ON_EXCHANGE: {key}")
        logger.warning("[RECONCILE] %s 거래소에서 청산됨 → 로컬 제거", key)
        local_pos = pos_mgr.positions.get(key)
        if local_pos:
            local_pos.phase = "CLOSED"
        pos_mgr.positions.pop(key, None)

    # 거래소에 있고 로컬에 없음 → 미추적
    for key in exchange_keys - local_keys:
        status = "MISMATCH"
        details.append(f"UNTRACKED: {key}")
        logger.critical(
            "[RECONCILE] %s 미추적 포지션 발견 — 수동 확인 필요", key,
        )

    # 수량 불일치
    for key in local_keys & exchange_keys:
        local_size = pos_mgr.positions[key].total_size
        exch_size = float(exchange_positions[key].get("contracts", 0) or 0)
        if abs(exch_size - local_size) / max(exch_size, 0.0001) > 0.05:
            status = "MISMATCH"
            details.append(
                f"QTY_MISMATCH: {key} local={local_size:.6f} exch={exch_size:.6f}"
            )
            logger.warning(
                "[RECONCILE] %s 수량 불일치: 로컬=%.6f 거래소=%.6f → 거래소 기준",
                key, local_size, exch_size,
            )
            pos_mgr.positions[key].total_size = exch_size

    # Step 4: 서버 SL 확인
    for key, pos in pos_mgr.positions.items():
        if pos.phase == "CLOSED":
            continue
        sym = pos.symbol
        try:
            orders = client.get_open_orders(sym)
            has_sl = any(
                float(o.get("triggerPrice") or o.get("stopPrice") or 0) > 0
                for o in (orders or [])
            )
            if not has_sl and pos.sl_price > 0:
                logger.critical(
                    "[RECONCILE] %s SL 없음 → 서버 SL 재설정", key,
                )
                place_server_sl(
                    sym, pos.sl_price, pos.total_size,
                    client, pos.side,
                )
        except Exception as e:
            logger.error("[RECONCILE] %s SL 확인 실패: %s", key, e)

    logger.info(
        "[RECONCILE] 완료: status=%s, details=%d건",
        status, len(details),
    )
    return {"status": status, "details": details}


# ══════════════════════════════════════════
# 9-D. Mark Price 감시
# ══════════════════════════════════════════

class MarkPriceMonitor:
    """Mark/Last price 괴리 감시."""

    ALERT_THRESHOLD = 0.003  # 0.3%
    CRITICAL_THRESHOLD = 0.008  # 0.8%

    def __init__(self):
        self._last_alert_ts: Dict[str, float] = {}

    def check(
        self, symbol: str, mark: float, last: float,
        sl_dist_pct: float = 0,
    ) -> Dict:
        """괴리 체크. 결과 dict 반환."""
        if last <= 0:
            return {"gap_pct": 0, "alert": False}

        gap = abs(mark - last) / last
        alert = False

        # 60초 이내 중복 알림 방지
        now = time.time()
        last_alert = self._last_alert_ts.get(symbol, 0)

        if gap > self.CRITICAL_THRESHOLD and now - last_alert > 60:
            logger.critical(
                "[MARK_GAP] %s CRITICAL: gap=%.4f%% mark=%.1f last=%.1f",
                symbol, gap * 100, mark, last,
            )
            self._last_alert_ts[symbol] = now
            alert = True
        elif gap > self.ALERT_THRESHOLD and now - last_alert > 60:
            logger.warning(
                "[MARK_GAP] %s WARNING: gap=%.4f%% mark=%.1f last=%.1f",
                symbol, gap * 100, mark, last,
            )
            self._last_alert_ts[symbol] = now
            alert = True

        return {"gap_pct": round(gap, 6), "alert": alert}
