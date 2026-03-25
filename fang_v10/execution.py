"""
안전한 주문 실행 모듈.

3단계 쿨다운, SL 등록 확인, 페이퍼 PnL, idempotency.
"""

from __future__ import annotations

import hashlib
import logging
import time
from typing import Dict, List, Optional

from fang_v10.config import CONFIG
from fang_v10.exchange_api import BitgetClient
from fang_v10.sizing_engine import SizingResult

logger = logging.getLogger(__name__)


class SafeExecutor:
    """안전한 주문 실행기. BitgetClient 래핑."""

    def __init__(self, client: BitgetClient) -> None:
        self.client = client
        self._last_trade: Dict[str, Dict] = {}  # {symbol: {"ts", "result", "side"}}
        self._idempotency_cache: Dict[str, float] = {}  # {hash: ts}
        self._IDEMPOTENCY_WINDOW: float = 10.0  # 10초

    # ──────────────────────────────────────────
    # 쿨다운
    # ──────────────────────────────────────────

    def _get_cooldown_sec(self, symbol: str, side: str) -> int:
        """3단계 쿨다운 시간 계산."""
        last = self._last_trade.get(symbol)
        if not last:
            return CONFIG.COOLDOWN_NORMAL_SEC

        if "sl" in last["result"].lower():
            if last["side"] == side:
                return CONFIG.COOLDOWN_AFTER_SL_SAME_DIR_SEC
            else:
                return CONFIG.COOLDOWN_AFTER_SL_SEC

        return CONFIG.COOLDOWN_NORMAL_SEC

    def _check_cooldown(self, symbol: str, side: str) -> Optional[str]:
        """쿨다운 체크. 통과면 None, 아니면 사유."""
        last = self._last_trade.get(symbol)
        if not last:
            return None

        cooldown_sec = self._get_cooldown_sec(symbol, side)
        elapsed = time.time() - last["ts"]
        if elapsed < cooldown_sec:
            remaining = cooldown_sec - elapsed
            return f"쿨다운 {remaining:.0f}초 남음 ({last['result']}→{side})"
        return None

    def record_trade_result(self, symbol: str, side: str, result: str) -> None:
        """거래 결과 기록 (쿨다운 판단용)."""
        self._last_trade[symbol] = {
            "ts": time.time(),
            "result": result,
            "side": side,
        }

    # ──────────────────────────────────────────
    # SL 등록 확인
    # ──────────────────────────────────────────

    def _verify_sl_registered(
        self, symbol: str, side: str, expected_trigger: float,
        max_retries: int = 3,
        client_order_id: Optional[str] = None,
        expected_amount: Optional[float] = None,
    ) -> bool:
        """SL 등록 확인. 3회 재시도 + 복합 매칭.

        검증 조건 (전부 AND, 순서대로):
          1. symbol 일치 (open_orders가 해당 symbol 대상)
          2. side == expected_close_side
          3. reduceOnly == True
          4. triggerPrice 또는 stopPrice 존재, 오차 < 1%
          5. amount 오차 < 5%
          6. (선택) clientOrderId 일치 — 있으면 추가 확인, 없어도 통과

        재시도: 300ms / 600ms / 900ms 간격으로 3회.
        """
        close_side = "sell" if side == "long" else "buy"

        for attempt in range(max_retries):
            time.sleep(0.3 * (attempt + 1))  # 300/600/900ms
            try:
                orders = self.client.get_open_orders(symbol)  # 1. symbol 일치
                for o in orders:
                    # 2. side 일치
                    if o.get("side", "") != close_side:
                        continue

                    # 3. reduceOnly
                    if not (o.get("reduceOnly", False) or o.get("reduce_only", False)):
                        continue

                    # 4. trigger price 오차 < 1%
                    trigger = float(
                        o.get("triggerPrice")
                        or o.get("stopPrice")
                        or 0
                    )
                    if trigger <= 0:
                        continue
                    if abs(trigger - expected_trigger) / expected_trigger >= 0.01:
                        continue

                    # 5. amount 오차 < 5%
                    if expected_amount and expected_amount > 0:
                        order_amt = float(
                            o.get("amount", 0) or o.get("contracts", 0) or 0
                        )
                        if order_amt <= 0:
                            continue
                        if abs(order_amt - expected_amount) / expected_amount >= 0.05:
                            continue

                    # 6. (선택) clientOrderId — 있으면 추가 확인, 없어도 통과
                    if client_order_id:
                        order_cid = o.get("clientOrderId") or o.get("clientOid") or ""
                        if order_cid and order_cid != client_order_id:
                            # 거래소가 반환했는데 불일치 → 다른 주문일 수 있음, skip
                            continue
                        # order_cid가 빈 문자열이면 (거래소 미반환) → 통과

                    return True  # 모든 AND 조건 충족
            except Exception as e:
                logger.warning("SL 확인 %d/%d 실패: %s", attempt + 1, max_retries, e)

        return False

    # ──────────────────────────────────────────
    # 안전한 진입
    # ──────────────────────────────────────────

    def safe_open(
        self, symbol: str, side: str, sizing: SizingResult,
    ) -> Optional[Dict]:
        """안전한 포지션 오픈.

        10단계 검증 후 주문 실행.
        """
        asset = "BTC" if "BTC" in symbol else "ETH"

        # 1. 쿨다운
        cd_reason = self._check_cooldown(symbol, side)
        if cd_reason:
            logger.info("진입 차단 (쿨다운): %s %s — %s", symbol, side, cd_reason)
            return None

        # 2. Idempotency
        idem_key = hashlib.md5(
            f"{symbol}|{side}|{sizing.notional:.2f}".encode()
        ).hexdigest()
        now = time.time()
        if idem_key in self._idempotency_cache:
            if now - self._idempotency_cache[idem_key] < self._IDEMPOTENCY_WINDOW:
                logger.warning("중복 요청 차단: %s %s", symbol, side)
                return None
        self._idempotency_cache[idem_key] = now

        # 페이퍼 모드
        if self.client.paper:
            return self._paper_open(symbol, side, sizing)

        # 3~4. 기존 포지션 확인
        existing = self.client.get_position(symbol)
        if existing and float(existing.get("contracts", 0) or 0) > 0:
            logger.info("이미 포지션 존재, 진입 차단: %s", symbol)
            return None

        # 5. 미체결 주문 확인
        open_orders = self.client.get_open_orders(symbol)
        if open_orders:
            logger.info("미체결 주문 존재, 진입 차단: %s (%d건)", symbol, len(open_orders))
            return None

        # 6. 레버리지/마진 설정
        self.client.ensure_ready(symbol, sizing.leverage)

        # 7. 수량 정밀도 + 최소 명목가
        amount = self.client.format_amount(symbol, sizing.amount)
        if amount <= 0:
            logger.warning("수량 0 이하, 진입 차단: %s amount=%.8f", symbol, amount)
            return None
        notional = amount * sizing.entry_price
        market_info = self.client.get_market_info(symbol)
        if notional < market_info.get("min_notional", 5):
            logger.warning("최소 명목가 미달: %.2f < %.2f", notional, market_info.get("min_notional", 5))
            return None

        # 8. 시장가 주문
        order_side = "buy" if side == "long" else "sell"
        try:
            order = self.client.create_market_order(symbol, order_side, amount)
        except Exception as e:
            logger.error("주문 실패 %s %s: %s", symbol, side, e)
            return None

        fill_price = float(order.get("average", 0) or sizing.entry_price)

        # 9. SL 설정 (clientOrderId 생성)
        sl_price = sizing.sl_price
        sl_formatted = self.client.format_price(symbol, sl_price)
        sl_client_order_id = f"sl_{symbol.replace('/', '_')}_{int(time.time())}"
        try:
            self.client.set_trigger_sl(
                symbol, side, sl_formatted, amount,
                client_order_id=sl_client_order_id,
            )
        except Exception as e:
            logger.critical("SL 설정 실패, 포지션 청산: %s — %s", symbol, e)
            self.safe_close(symbol, side, "SL 설정 실패")
            return None

        # 10. SL 등록 확인 (clientOrderId + 수량 매칭)
        if not self._verify_sl_registered(
            symbol, side, sl_formatted,
            client_order_id=sl_client_order_id,
            expected_amount=amount,
        ):
            logger.critical("SL 미등록 3회 실패, 포지션 청산: %s", symbol)
            self.safe_close(symbol, side, "SL 미등록")
            return None

        # 11. 1초 후 포지션 크기 검증
        time.sleep(1)
        pos = self.client.get_position(symbol)

        # 12. 실제 청산가 검증
        if pos:
            actual_liq = float(pos.get("liquidationPrice", 0) or 0)
            if actual_liq > 0:
                entry_price = fill_price
                actual_liq_dist = abs(entry_price - actual_liq) / entry_price
                sl_dist_pct = abs(entry_price - sl_price) / entry_price

                if actual_liq_dist <= 0:
                    logger.critical("청산가 계산 이상 → 비상 청산: %s", symbol)
                    self.safe_close(symbol, side, "LIQ_CALC_ERROR")
                    return None

                ratio = sl_dist_pct / actual_liq_dist

                if ratio > 0.70:
                    logger.critical(
                        "SL/청산비율 %.2f > 0.70 → 비상 청산! "
                        "SL거리=%.2f%% 청산거리=%.2f%%",
                        ratio, sl_dist_pct * 100, actual_liq_dist * 100,
                    )
                    self.safe_close(symbol, side, "LIQ_TOO_CLOSE")
                    return None

        logger.info(
            "포지션 오픈: %s %s @ %.2f, 수량=%.6f, 레버리지=%dx",
            symbol, side, fill_price, amount, sizing.leverage,
        )
        return {
            "symbol": symbol, "side": side,
            "fill_price": fill_price, "amount": amount,
            "leverage": sizing.leverage, "order": order,
        }

    # ──────────────────────────────────────────
    # 안전한 부분 청산
    # ──────────────────────────────────────────

    def safe_partial_close(
        self, symbol: str, side: str, ratio: float, reason: str,
    ) -> Optional[Dict]:
        """부분 청산.

        잔량이 최소주문 이하면 트리거 주문 즉시 취소.
        """
        asset = "BTC" if "BTC" in symbol else "ETH"

        if self.client.paper:
            return self._paper_partial_close(symbol, side, ratio, reason)

        pos = self.client.get_position(symbol)
        if not pos:
            logger.warning("부분 청산 실패 — 포지션 없음: %s", symbol)
            return None

        total_amount = float(pos.get("contracts", 0) or 0)
        close_amount = self.client.format_amount(symbol, total_amount * ratio)

        if close_amount <= 0:
            return None

        close_side = "sell" if side == "long" else "buy"
        try:
            order = self.client.create_market_order(
                symbol, close_side, close_amount, reduce_only=True,
            )
        except Exception as e:
            logger.error("부분 청산 실패 %s: %s", symbol, e)
            return None

        # 잔량 확인 → 최소주문 이하면 트리거 취소
        remaining = max(0.0, total_amount - close_amount)
        min_amount = {"BTC": 0.001, "ETH": 0.01}.get(asset, 0.001)
        if remaining < min_amount:
            self.client.cancel_trigger_orders(symbol)

        # SL 수량 갱신 (잔량에 맞게)
        if remaining >= min_amount:
            sl_price = float(pos.get("stopLossPrice", 0) or 0)
            if sl_price > 0:
                try:
                    self.client.cancel_trigger_orders(symbol)
                    formatted_remaining = self.client.format_amount(symbol, remaining)
                    self.client.set_trigger_sl(
                        symbol, side, sl_price, formatted_remaining,
                    )
                    if not self._verify_sl_registered(symbol, side, sl_price):
                        raise Exception("SL 재설정 확인 실패")
                except Exception as e:
                    logger.critical(
                        "부분청산 후 SL 재설정 실패 → 잔량 비상청산: %s — %s", symbol, e,
                    )
                    self.safe_close(symbol, side, "SL_RESET_FAIL")

        logger.info(
            "%s %s 부분청산 %.1f%% (%s): 수량=%.6f",
            symbol, side, ratio * 100, reason, close_amount,
        )
        return {
            "symbol": symbol, "side": side,
            "close_amount": close_amount, "remaining": remaining,
            "reason": reason, "order": order,
        }

    # ──────────────────────────────────────────
    # 안전한 전체 청산
    # ──────────────────────────────────────────

    def safe_close(
        self, symbol: str, side: str, reason: str,
    ) -> Optional[Dict]:
        """전체 청산."""
        if self.client.paper:
            return self._paper_close(symbol, side, reason)

        pos = self.client.get_position(symbol)
        if not pos:
            return None

        amount = float(pos.get("contracts", 0) or 0)
        if amount <= 0:
            return None

        close_side = "sell" if side == "long" else "buy"

        # 트리거 주문 취소
        self.client.cancel_trigger_orders(symbol)

        try:
            order = self.client.create_market_order(
                symbol, close_side, amount, reduce_only=True,
            )
        except Exception as e:
            logger.error("전체 청산 실패 %s: %s", symbol, e)
            return None

        logger.info("%s %s 전체청산 (%s)", symbol, side, reason)
        return {
            "symbol": symbol, "side": side,
            "close_amount": amount, "reason": reason, "order": order,
        }

    # ──────────────────────────────────────────
    # 안전한 DCA
    # ──────────────────────────────────────────

    def safe_dca(
        self, symbol: str, side: str, dca_sizing: SizingResult,
    ) -> Optional[Dict]:
        """DCA 추가 매수."""
        if self.client.paper:
            return self._paper_dca(symbol, side, dca_sizing)

        # 기존 포지션 확인
        pos = self.client.get_position(symbol)
        if not pos or float(pos.get("contracts", 0) or 0) <= 0:
            logger.warning("DCA 실패 — 기존 포지션 없음: %s", symbol)
            return None

        amount = self.client.format_amount(symbol, dca_sizing.amount)
        order_side = "buy" if side == "long" else "sell"

        try:
            order = self.client.create_market_order(symbol, order_side, amount)
        except Exception as e:
            logger.error("DCA 주문 실패 %s: %s", symbol, e)
            return None

        # SL 재설정 (전체 수량)
        total = float(pos.get("contracts", 0) or 0) + amount
        try:
            self.client.cancel_trigger_orders(symbol)
            formatted_total = self.client.format_amount(symbol, total)
            self.client.set_trigger_sl(
                symbol, side, dca_sizing.sl_price, formatted_total,
            )
            if not self._verify_sl_registered(symbol, side, dca_sizing.sl_price):
                raise Exception("DCA 후 SL 확인 실패")
        except Exception as e:
            logger.critical(
                "DCA 후 SL 재설정 실패 → 전체 비상청산: %s — %s", symbol, e,
            )
            self.safe_close(symbol, side, "DCA_SL_FAIL")
            return None

        logger.info("DCA 실행: %s %s, 추가수량=%.6f", symbol, side, amount)
        return {
            "symbol": symbol, "side": side,
            "dca_amount": amount, "total_amount": total, "order": order,
        }

    # ──────────────────────────────────────────
    # 페이퍼 모드
    # ──────────────────────────────────────────

    def _paper_open(
        self, symbol: str, side: str, sizing: SizingResult,
    ) -> Dict:
        """페이퍼 모드 진입."""
        asset = "BTC" if "BTC" in symbol else "ETH"
        slippage = CONFIG.SLIPPAGE.get(asset, 0.0003)

        if side == "long":
            fill_price = sizing.entry_price * (1 + slippage)
        else:
            fill_price = sizing.entry_price * (1 - slippage)

        logger.info(
            "[PAPER] 포지션 오픈: %s %s @ %.2f (slip %.4f%%)",
            symbol, side, fill_price, slippage * 100,
        )
        return {
            "symbol": symbol, "side": side,
            "fill_price": fill_price, "amount": sizing.amount,
            "leverage": sizing.leverage, "order": {"id": "paper", "status": "filled"},
        }

    def _paper_partial_close(
        self, symbol: str, side: str, ratio: float, reason: str,
    ) -> Dict:
        """페이퍼 부분 청산."""
        logger.info("[PAPER] %s %s 부분청산 %.1f%% (%s)", symbol, side, ratio * 100, reason)
        return {
            "symbol": symbol, "side": side,
            "close_amount": 0, "remaining": 0,
            "reason": reason, "order": {"id": "paper", "status": "filled"},
        }

    def _paper_close(
        self, symbol: str, side: str, reason: str,
    ) -> Dict:
        """페이퍼 전체 청산."""
        logger.info("[PAPER] %s %s 전체청산 (%s)", symbol, side, reason)
        return {
            "symbol": symbol, "side": side,
            "close_amount": 0, "reason": reason,
            "order": {"id": "paper", "status": "filled"},
        }

    def _paper_dca(
        self, symbol: str, side: str, dca_sizing: SizingResult,
    ) -> Dict:
        """페이퍼 DCA."""
        logger.info("[PAPER] DCA: %s %s, 수량=%.6f", symbol, side, dca_sizing.amount)
        return {
            "symbol": symbol, "side": side,
            "dca_amount": dca_sizing.amount, "total_amount": 0,
            "order": {"id": "paper", "status": "filled"},
        }

    @staticmethod
    def calc_paper_pnl(
        side: str, entry_price: float, exit_price: float,
        amount: float, asset: str,
    ) -> Dict:
        """페이퍼 PnL 계산 (롱/숏 완전 대칭).

        Returns:
            {"gross_pnl": float, "fee": float, "net_pnl": float}
        """
        slippage = CONFIG.SLIPPAGE.get(asset, 0.0003)

        if side == "long":
            entry_fill = entry_price * (1 + slippage)
            exit_fill = exit_price * (1 - slippage)
            side_sign = 1
        else:
            entry_fill = entry_price * (1 - slippage)
            exit_fill = exit_price * (1 + slippage)
            side_sign = -1

        gross_pnl = side_sign * (exit_fill - entry_fill) * amount
        fee = (entry_fill + exit_fill) * amount * CONFIG.EFFECTIVE_TAKER_FEE
        net_pnl = gross_pnl - fee

        return {
            "gross_pnl": round(gross_pnl, 4),
            "fee": round(fee, 4),
            "net_pnl": round(net_pnl, 4),
        }
