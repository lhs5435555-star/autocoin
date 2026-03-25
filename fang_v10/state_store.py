"""
상태 영속 저장소.

포지션/리스크/MDD 상태 JSON 직렬화, 거래소 Reconcile, 거래 로그.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from fang_v10.config import CONFIG

logger = logging.getLogger(__name__)


class StateStore:
    """상태 영속 저장소. ~/.fang_v10/state.json"""

    def __init__(self, data_dir: Optional[str] = None) -> None:
        self.data_dir = Path(data_dir or CONFIG.DATA_DIR)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.state_file = self.data_dir / "state.json"
        self.trades_log_file = self.data_dir / "trades_log.jsonl"

    # ──────────────────────────────────────────
    # 상태 저장 / 로드
    # ──────────────────────────────────────────

    def save(
        self,
        positions: Dict[str, Any],
        risk_state: Dict[str, Any],
        mdd_state: Dict[str, Any],
    ) -> None:
        """상태 JSON 저장."""
        state = {
            "timestamp": time.time(),
            "positions": positions,
            "risk_state": risk_state,
            "mdd_state": mdd_state,
        }
        try:
            tmp = self.state_file.with_suffix(".tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(state, f, ensure_ascii=False, indent=2, default=str)
            tmp.replace(self.state_file)
            logger.debug("상태 저장 완료: %s", self.state_file)
        except Exception as e:
            logger.error("상태 저장 실패: %s", e)

    def load(self) -> Tuple[Dict, Dict, Dict]:
        """상태 로드.

        Returns:
            (positions, risk_state, mdd_state) — 파일 없으면 빈 기본값
        """
        if not self.state_file.exists():
            logger.info("상태 파일 없음, 빈 기본값 반환")
            return {}, {}, {}

        try:
            with open(self.state_file, "r", encoding="utf-8") as f:
                state = json.load(f)
            logger.info("상태 로드 완료 (ts=%.0f)", state.get("timestamp", 0))
            return (
                state.get("positions", {}),
                state.get("risk_state", {}),
                state.get("mdd_state", {}),
            )
        except (json.JSONDecodeError, OSError) as e:
            logger.error("상태 로드 실패: %s", e)
            return {}, {}, {}

    # ──────────────────────────────────────────
    # Reconcile (거래소 ↔ 로컬)
    # ──────────────────────────────────────────

    def reconcile(
        self,
        local_positions: Dict[str, Any],
        exchange_positions: List[Dict],
        client: Any,  # BitgetClient
        risk_engine: Any,  # RiskEngine
    ) -> Dict[str, Any]:
        """거래소 기준 포지션 보정.

        - 거래소에 있고 로컬에 없음 → 거래소 기준 복원 (경고)
        - 로컬에 있고 거래소에 없음 → SL 체결 PnL 확인 후 제거
        - 수량 불일치 → 거래소 기준
        - 모든 포지션의 서버사이드 SL 존재 확인

        Returns:
            보정된 local_positions
        """
        # 거래소 포지션 매핑
        exchange_map: Dict[str, Dict] = {}
        for ep in exchange_positions:
            sym = ep.get("symbol", "")
            side = ep.get("side", "")
            size = float(ep.get("contracts", 0) or 0)
            if size > 0 and sym and side:
                key = f"{sym}|{side}"
                exchange_map[key] = ep

        reconciled = dict(local_positions)

        # 거래소에 있고 로컬에 없음 → 복원
        for key, ep in exchange_map.items():
            if key not in reconciled:
                logger.warning("Reconcile: 거래소에만 존재 → 복원: %s", key)
                reconciled[key] = {
                    "symbol": ep.get("symbol"),
                    "side": ep.get("side"),
                    "avg_price": float(ep.get("entryPrice", 0) or 0),
                    "total_size": float(ep.get("contracts", 0) or 0),
                    "leverage": int(ep.get("leverage", 1) or 1),
                    "reconciled": True,
                }

        # 로컬에 있고 거래소에 없음 → SL 체결 확인 후 제거
        keys_to_remove = []
        for key in list(reconciled.keys()):
            if key not in exchange_map:
                parts = key.split("|")
                if len(parts) >= 1:
                    symbol = parts[0]
                    # SL 체결 PnL 확인
                    pnl = self._check_sl_fill_pnl(symbol, client)
                    if pnl is not None:
                        risk_engine.record_trade(pnl, symbol)
                        logger.info(
                            "Reconcile: SL 체결 확인 %s, PnL=%.2f USDT", symbol, pnl,
                        )
                    else:
                        logger.warning("Reconcile: 로컬에만 존재, 제거: %s", key)
                keys_to_remove.append(key)

        for key in keys_to_remove:
            del reconciled[key]

        # 수량 불일치 → 거래소 기준
        for key in reconciled:
            if key in exchange_map:
                ex_size = float(exchange_map[key].get("contracts", 0) or 0)
                local_size = reconciled[key].get("total_size", 0)
                if abs(ex_size - local_size) / max(ex_size, 0.0001) > 0.01:
                    logger.warning(
                        "Reconcile: 수량 불일치 %s: 로컬=%.6f, 거래소=%.6f → 거래소 기준",
                        key, local_size, ex_size,
                    )
                    reconciled[key]["total_size"] = ex_size

        # 서버사이드 SL 존재 확인
        for key, ep in exchange_map.items():
            if key in reconciled:
                symbol = ep.get("symbol", "")
                side = reconciled[key].get("side", "long")
                try:
                    orders = client.get_open_orders(symbol)
                    has_sl = any(
                        float(o.get("triggerPrice") or o.get("stopPrice") or 0) > 0
                        for o in orders
                    )
                    if not has_sl:
                        logger.critical("Reconcile: SL 미설정 발견 → 즉시 설정: %s", key)
                        # SL 가격 계산 (avg_price 기반)
                        avg = reconciled[key].get("avg_price", 0)
                        r_dist = reconciled[key].get("initial_r_distance", 0)
                        if avg > 0 and r_dist > 0:
                            if side == "long":
                                sl_price = avg - r_dist
                            else:
                                sl_price = avg + r_dist
                            size = float(ep.get("contracts", 0) or 0)
                            client.set_trigger_sl(symbol, side, sl_price, size)
                except Exception as e:
                    logger.error("Reconcile: SL 확인 실패 %s: %s", key, e)

        return reconciled

    def _check_sl_fill_pnl(self, symbol: str, client: Any) -> Optional[float]:
        """최근 거래에서 SL 체결 PnL 확인."""
        try:
            since = int((time.time() - 3600) * 1000)  # 최근 1시간
            trades = client.fetch_my_trades(symbol, since=since)
            if not trades:
                return None

            # 가장 최근 거래의 PnL
            last_trade = trades[-1]
            pnl = float(last_trade.get("info", {}).get("profit", 0) or 0)
            return pnl if pnl != 0 else None
        except Exception as e:
            logger.error("SL 체결 PnL 확인 실패 %s: %s", symbol, e)
            return None

    # ──────────────────────────────────────────
    # 거래 로그
    # ──────────────────────────────────────────

    def save_trade_log(self, trade_dict: Dict) -> None:
        """trades_log.jsonl에 거래 레코드 append."""
        try:
            with open(self.trades_log_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(trade_dict, ensure_ascii=False, default=str) + "\n")
        except Exception as e:
            logger.error("거래 로그 저장 실패: %s", e)
