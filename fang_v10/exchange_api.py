"""
Bitget USDT-M V2 거래소 API 래퍼.

ccxt 기반, rate limit, 재시도, 정밀도 처리.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional

import ccxt
import pandas as pd

logger = logging.getLogger(__name__)


class BitgetClient:
    """Bitget USDT-M 선물 클라이언트."""

    # Rate limit
    _MIN_REQUEST_INTERVAL: float = 0.1  # 100ms

    def __init__(
        self,
        api_key: str = "",
        secret: str = "",
        passphrase: str = "",
        paper: bool = True,
    ) -> None:
        self.paper = paper
        self._last_request_ts: float = 0.0
        self._consecutive_429: int = 0

        self.exchange = ccxt.bitget({
            "apiKey": api_key,
            "secret": secret,
            "password": passphrase,
            "options": {
                "defaultType": "swap",
                "productType": "USDT-FUTURES",
            },
            "enableRateLimit": True,
        })

        if paper:
            self.exchange.set_sandbox_mode(True)

    # ──────────────────────────────────────────
    # 초기 설정
    # ──────────────────────────────────────────

    def ensure_ready(self, symbol: str, leverage: int) -> int:
        """포지션 모드/마진/레버리지 설정. 3회 재시도.

        Returns:
            설정된 레버리지
        """
        for step_name, fn in [
            ("position_mode", lambda: self.exchange.set_position_mode(False, symbol)),
            ("margin_mode", lambda: self.exchange.set_margin_mode("isolated", symbol, {"leverage": leverage})),
            ("leverage", lambda: self.exchange.set_leverage(leverage, symbol)),
        ]:
            self._retry(fn, label=f"ensure_ready/{step_name}/{symbol}")

        return leverage

    # ──────────────────────────────────────────
    # 포지션 조회
    # ──────────────────────────────────────────

    def get_position(self, symbol: str) -> Optional[Dict]:
        """현재 포지션 조회. 없으면 None."""
        def _fetch():
            positions = self.exchange.fetch_positions([symbol])
            for p in positions:
                size = float(p.get("contracts", 0) or 0)
                if size > 0:
                    return p
            return None

        try:
            return self._retry(_fetch, label=f"get_position/{symbol}")
        except Exception as e:
            logger.error("get_position 완전 실패 %s: %s", symbol, e)
            return None

    # ──────────────────────────────────────────
    # 주문
    # ──────────────────────────────────────────

    def create_market_order(
        self, symbol: str, side: str, amount: float, reduce_only: bool = False,
    ) -> Dict:
        """시장가 주문."""
        params = {}
        if reduce_only:
            params["reduceOnly"] = True

        def _order():
            return self.exchange.create_order(
                symbol=symbol, type="market", side=side,
                amount=amount, params=params,
            )

        result = self._retry(_order, label=f"market_order/{symbol}/{side}")
        return result

    def set_trigger_sl(
        self, symbol: str, side: str, trigger_price: float, amount: float,
        client_order_id: Optional[str] = None,
    ) -> Dict:
        """트리거 SL 설정 (mark price 기준).

        ccxt 버전 호환: triggerPrice 실패 시 stopLossPrice로 재시도.
        client_order_id가 주어지면 주문에 포함 (SL 검증용).
        """
        close_side = "sell" if side == "long" else "buy"
        formatted_price = self.format_price(symbol, trigger_price)
        formatted_amount = self.format_amount(symbol, amount)

        # 1차 시도: triggerPrice
        primary_params: Dict[str, Any] = {
            "triggerPrice": formatted_price,
            "triggerType": "mark_price",
            "reduceOnly": True,
        }
        if client_order_id:
            primary_params["clientOid"] = client_order_id

        # 시도 순서: triggerPrice+cid → triggerPrice-cid → stopLoss+cid → stopLoss-cid
        attempts = [
            ("triggerPrice", primary_params),
        ]

        # clientOrderId 거부 시 fallback: cid 없이 재시도
        if client_order_id:
            no_cid_primary = {k: v for k, v in primary_params.items() if k != "clientOid"}
            attempts.append(("triggerPrice(no-cid)", no_cid_primary))

        fallback_params: Dict[str, Any] = {
            "stopLossPrice": formatted_price,
            "reduceOnly": True,
        }
        if client_order_id:
            fallback_params["clientOid"] = client_order_id
            attempts.append(("stopLossPrice", fallback_params))
            no_cid_fallback = {k: v for k, v in fallback_params.items() if k != "clientOid"}
            attempts.append(("stopLossPrice(no-cid)", no_cid_fallback))
        else:
            attempts.append(("stopLossPrice", fallback_params))

        last_err = None
        for label, params in attempts:
            try:
                self._rate_limit()
                result = self.exchange.create_order(
                    symbol=symbol, type="market", side=close_side,
                    amount=formatted_amount, params=params,
                )
                logger.info("SL 설정 성공 (%s): %s %s @ %s", label, symbol, close_side, formatted_price)
                return result
            except Exception as e:
                logger.warning("SL %s 실패: %s", label, e)
                last_err = e

        logger.error("SL 설정 완전 실패 %s: %s", symbol, last_err)
        raise last_err or RuntimeError(f"SL 설정 실패: {symbol}")

    def cancel_trigger_orders(self, symbol: str) -> None:
        """심볼의 모든 트리거 주문 취소."""
        try:
            orders = self.get_open_orders(symbol)
            for o in orders:
                order_id = o.get("id")
                if order_id:
                    self._rate_limit()
                    self.exchange.cancel_order(order_id, symbol)
                    logger.info("트리거 주문 취소: %s %s", symbol, order_id)
        except Exception as e:
            logger.error("트리거 주문 취소 실패 %s: %s", symbol, e)

    def get_open_orders(self, symbol: str) -> List[Dict]:
        """미체결 주문 조회."""
        def _fetch():
            return self.exchange.fetch_open_orders(symbol)

        try:
            return self._retry(_fetch, label=f"open_orders/{symbol}")
        except Exception:
            return []

    # ──────────────────────────────────────────
    # 잔고 / OHLCV / 거래내역
    # ──────────────────────────────────────────

    def fetch_balance(self) -> float:
        """USDT 가용 잔고."""
        def _fetch():
            bal = self.exchange.fetch_balance()
            return float(bal.get("USDT", {}).get("free", 0) or 0)

        return self._retry(_fetch, label="fetch_balance")

    def fetch_ohlcv(
        self, symbol: str, timeframe: str = "5m", limit: int = 500,
    ) -> pd.DataFrame:
        """OHLCV 데이터 DataFrame 반환."""
        def _fetch():
            data = self.exchange.fetch_ohlcv(symbol, timeframe, limit=limit)
            df = pd.DataFrame(data, columns=["timestamp", "open", "high", "low", "close", "volume"])
            df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
            return df

        return self._retry(_fetch, label=f"ohlcv/{symbol}/{timeframe}")

    def fetch_my_trades(self, symbol: str, since: Optional[int] = None) -> List[Dict]:
        """내 거래내역 조회 (reconcile용)."""
        def _fetch():
            return self.exchange.fetch_my_trades(symbol, since=since, limit=100)

        try:
            return self._retry(_fetch, label=f"my_trades/{symbol}")
        except Exception:
            return []

    # ──────────────────────────────────────────
    # 정밀도 / 시장 정보
    # ──────────────────────────────────────────

    def get_market_info(self, symbol: str) -> Dict:
        """시장 정밀도 및 최소 주문 정보."""
        market = self.exchange.market(symbol)
        return {
            "price_precision": market["precision"]["price"],
            "amount_precision": market["precision"]["amount"],
            "min_amount": market["limits"]["amount"]["min"],
            "min_notional": market["limits"].get("cost", {}).get("min", 5),
        }

    def format_amount(self, symbol: str, amount: float) -> float:
        """거래소 정밀도에 맞춰 수량 포맷."""
        return float(self.exchange.amount_to_precision(symbol, amount))

    def format_price(self, symbol: str, price: float) -> float:
        """거래소 정밀도에 맞춰 가격 포맷."""
        return float(self.exchange.price_to_precision(symbol, price))

    # ──────────────────────────────────────────
    # Rate limit / 재시도
    # ──────────────────────────────────────────

    def _rate_limit(self) -> None:
        """최소 요청 간격 보장."""
        elapsed = time.time() - self._last_request_ts
        if elapsed < self._MIN_REQUEST_INTERVAL:
            time.sleep(self._MIN_REQUEST_INTERVAL - elapsed)
        self._last_request_ts = time.time()

    def _retry(self, fn, label: str = "", max_retries: int = 3):
        """3회 재시도 + exponential backoff.

        429 → 1,2,4,8초 backoff, 5연속 → 60초 대기
        네트워크/5xx → 3회 재시도
        4xx (429 제외) → 즉시 실패
        """
        last_err = None
        for attempt in range(max_retries):
            self._rate_limit()
            t0 = time.time()
            try:
                result = fn()
                elapsed = time.time() - t0
                logger.debug("[%s] %.3fs", label, elapsed)
                self._consecutive_429 = 0
                return result
            except ccxt.RateLimitExceeded as e:
                self._consecutive_429 += 1
                if self._consecutive_429 >= 5:
                    logger.warning("[%s] 429 5연속 → 60초 대기", label)
                    time.sleep(60)
                    self._consecutive_429 = 0
                else:
                    wait = 2 ** attempt
                    logger.warning("[%s] 429 → %d초 대기 (%d/5)", label, wait, self._consecutive_429)
                    time.sleep(wait)
                last_err = e
            except (ccxt.NetworkError, ccxt.ExchangeNotAvailable) as e:
                wait = 2 ** attempt
                logger.warning("[%s] 네트워크 오류 → %d초 후 재시도 (%d/%d): %s",
                               label, wait, attempt + 1, max_retries, e)
                time.sleep(wait)
                last_err = e
            except ccxt.ExchangeError as e:
                # 4xx (429 제외) → 즉시 실패
                logger.error("[%s] 거래소 오류 (재시도 안함): %s", label, e)
                raise
            except Exception as e:
                logger.error("[%s] 예외: %s", label, e)
                last_err = e
                if attempt < max_retries - 1:
                    time.sleep(2 ** attempt)

        raise last_err or RuntimeError(f"[{label}] {max_retries}회 재시도 실패")
