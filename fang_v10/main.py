"""
fang_v10 메인 루프.

Bitget USDT-M 선물 자동매매 봇.
"""

from __future__ import annotations

import asyncio
import logging

from fang_v10.config import CONFIG
from fang_v10.exchange_api import BitgetClient
from fang_v10.execution import SafeExecutor
from fang_v10.monitor import (
    log_blocked_stats,
    log_exit,
    log_kill_switch,
    log_phantom_stats,
    log_state,
    setup_logging,
)
from fang_v10.position_manager import PositionManager
from fang_v10.regime_engine import RegimeEngine, ensure_indicators
from fang_v10.risk_engine import MddTracker, RiskEngine
from fang_v10.sizing_engine import calc_position_size, check_dca_risk_gate
from fang_v10.state_store import (
    StateStore, restore_positions, restore_risk_state, restore_mdd_state,
)
from fang_v10.strategy import generate_signals

logger = logging.getLogger("fang_v10")


async def main_loop() -> None:
    """메인 트레이딩 루프."""
    # ── 초기화 ──
    setup_logging()
    logger.info("fang_v10 시작 (paper=%s)", CONFIG.PAPER_TRADING)

    client = BitgetClient(
        CONFIG.API_KEY, CONFIG.API_SECRET,
        CONFIG.PASSPHRASE, CONFIG.PAPER_TRADING,
    )
    executor = SafeExecutor(client)
    regime_eng = RegimeEngine()
    pos_mgr = PositionManager()
    risk_eng = RiskEngine()
    mdd = MddTracker()
    state = StateStore()

    # ── 재시작 복구 ──
    if not CONFIG.PAPER_TRADING:
        balance = client.fetch_balance()
    else:
        balance = 198.0

    risk_eng.set_initial_balance(balance)
    saved_pos, saved_risk, saved_mdd = state.load()

    # 포지션/리스크/MDD 복원
    if saved_pos:
        restore_positions(saved_pos, pos_mgr)
    restore_risk_state(saved_risk, risk_eng)
    restore_mdd_state(saved_mdd, mdd, balance)

    # 거래소 Reconcile
    if not CONFIG.PAPER_TRADING:
        exchange_positions = []
        for s in CONFIG.SYMBOLS:
            pos = client.get_position(s)
            if pos:
                exchange_positions.append(pos)
        saved_pos = state.reconcile(saved_pos, exchange_positions, client, risk_eng)

    loop_count = 0
    last_entry_candle: dict[str, int | None] = {s: None for s in CONFIG.SYMBOLS}

    while True:
        try:
            # BTC 데이터 먼저 확보 (ETH BTC필터에 필요)
            btc_df = None
            btc_symbol = next(
                (s for s in CONFIG.SYMBOLS if "BTC" in s), None,
            )
            if btc_symbol:
                try:
                    btc_df = client.fetch_ohlcv(
                        btc_symbol, CONFIG.TIMEFRAME_PRIMARY, 300,
                    )
                    if btc_df is not None and not btc_df.empty:
                        btc_df = ensure_indicators(btc_df)
                    else:
                        btc_df = None
                except Exception:
                    btc_df = None

            for symbol in CONFIG.SYMBOLS:
                # ── 1. 캔들 갱신 (5m) ──
                if "BTC" in symbol and btc_df is not None:
                    df = btc_df
                else:
                    df = client.fetch_ohlcv(symbol, CONFIG.TIMEFRAME_PRIMARY, 300)
                    if df is None or df.empty:
                        continue
                    df = ensure_indicators(df)

                bar_idx = len(df) - 1
                price = float(df.iloc[-1]["close"])

                # ── 2. 레짐 판별 ──
                regime = regime_eng.detect(symbol, df, bar_idx)

                # ── 3. Phantom/Blocked 업데이트 ──
                pos_mgr.phantom.update(symbol, price, bar_idx)
                pos_mgr.blocked.update(symbol, price, bar_idx)

                # ── 4. 기존 포지션 관리 ──
                ema9 = df.iloc[-1].get("ema9")
                ema21 = df.iloc[-1].get("ema21")

                for key, pos in list(pos_mgr.positions.items()):
                    if pos.symbol != symbol:
                        continue

                    exit_result = pos_mgr.check_exit(
                        key, price, bar_idx,
                        ema9=ema9, ema21=ema21, df=df,
                    )

                    if exit_result:
                        action = exit_result["action"]
                        reason = exit_result["reason"]
                        asset = "BTC" if "BTC" in symbol else "ETH"

                        if action == "close":
                            close_size = pos.total_size * pos.remaining_ratio
                            result = executor.safe_close(symbol, pos.side, reason)
                            if result:
                                if CONFIG.PAPER_TRADING:
                                    pnl = SafeExecutor.calc_paper_pnl(
                                        pos.side, pos.avg_price, price,
                                        close_size, asset,
                                    )["net_pnl"]
                                else:
                                    if pos.side == "long":
                                        gross = (price - pos.avg_price) * close_size
                                    else:
                                        gross = (pos.avg_price - price) * close_size
                                    fee = close_size * pos.avg_price * 2 * CONFIG.EFFECTIVE_TAKER_FEE
                                    pnl = gross - fee
                                # 최종청산 시 부분청산 누적분 합산
                                total_pnl = pnl + pos.realized_pnl
                                r_val = pos.calc_risk_r(price)
                                risk_eng.record_trade(total_pnl, symbol)
                                executor.record_trade_result(symbol, pos.side, reason.lower())
                                balance += pnl  # 잔고엔 이번 청산분만 (부분분은 이미 반영됨)

                                record = log_exit(
                                    symbol, pos.side, price, reason,
                                    f"{reason}: R={r_val:.2f}",
                                    total_pnl, r_val, pos.peak_r, pos.trough_r,
                                    bar_idx - pos.entry_bar, balance,
                                    regime=pos.regime, strategy=pos.strategy,
                                    entry_price=pos.avg_price,
                                )
                                state.save_trade_log(record)
                                del pos_mgr.positions[key]

                        elif action == "partial":
                            ratio = exit_result["ratio"]
                            partial_size = pos.total_size * pos.remaining_ratio * ratio
                            result = executor.safe_partial_close(
                                symbol, pos.side, ratio, reason,
                            )
                            if result:
                                # 부분청산 PnL: realized_pnl에만 누적 (record_trade는 최종청산 시)
                                if pos.side == "long":
                                    partial_gross = (price - pos.avg_price) * partial_size
                                else:
                                    partial_gross = (pos.avg_price - price) * partial_size
                                partial_fee = partial_size * pos.avg_price * 2 * CONFIG.EFFECTIVE_TAKER_FEE
                                partial_pnl = partial_gross - partial_fee
                                pos.realized_pnl += partial_pnl
                                balance += partial_pnl

                                pos.tp_count += 1
                                pos.remaining_ratio -= ratio
                                pos.total_size *= (1 - ratio)

                        elif action == "move_sl":
                            new_sl = exit_result.get("new_sl")
                            if new_sl and not CONFIG.PAPER_TRADING:
                                try:
                                    client.cancel_trigger_orders(symbol)
                                    client.set_trigger_sl(
                                        symbol, pos.side, new_sl, pos.total_size,
                                    )
                                except Exception as e:
                                    logger.error("BE SL 이동 실패: %s", e)

                    # DCA 체크
                    atr = df.iloc[-1].get("atr", 0)
                    dca_result = pos_mgr.check_dca(
                        symbol, pos.side, price, atr, bar_idx,
                        ema9=ema9, ema21=ema21,
                    )
                    if dca_result:
                        open_positions = [
                            {"symbol": pp.symbol,
                             "margin": pp.total_size * pp.remaining_ratio,
                             "initial_risk_1r_usd": (
                                 pp.total_size * pp.remaining_ratio
                                 * pp.initial_r_distance / pp.avg_price
                             ) if pp.avg_price > 0 else 0}
                            for pp in pos_mgr.positions.values()
                        ]
                        dca_margin = pos.total_size * CONFIG.DCA_SIZE_RATIO
                        allowed, gate_reason = check_dca_risk_gate(
                            symbol, dca_margin, balance, open_positions,
                        )
                        if not allowed:
                            logger.info("DCA 거부: %s — %s", symbol, gate_reason)
                            continue

                        sizing = calc_position_size(
                            balance, price, pos.avg_price - pos.initial_r_distance,
                            pos.avg_price + pos.initial_r_distance * CONFIG.TP1_R,
                            symbol,
                        )
                        if sizing.valid:
                            result = executor.safe_dca(symbol, pos.side, sizing)
                            if result:
                                pos.add_dca(price, sizing.amount)
                                # DCA 후 새 평균가 기준 SL 재설정
                                if not CONFIG.PAPER_TRADING:
                                    if pos.side == "long":
                                        new_sl = pos.avg_price - pos.initial_r_distance
                                    else:
                                        new_sl = pos.avg_price + pos.initial_r_distance
                                    try:
                                        client.cancel_trigger_orders(symbol)
                                        new_amount = pos.total_size * pos.remaining_ratio
                                        client.set_trigger_sl(
                                            symbol, pos.side, new_sl, new_amount,
                                        )
                                    except Exception as e:
                                        logger.critical("DCA SL 재설정 실패 → 비상청산: %s", e)
                                        executor.safe_close(symbol, pos.side, "DCA_SL_FAIL")

                # ── 5. 킬스위치 + MDD ──
                if not risk_eng.can_trade():
                    mode = risk_eng.get_risk_mode()
                    log_kill_switch(mode["reason"], 0)
                    continue

                can_coin, coin_reason = risk_eng.can_trade_coin(symbol)
                if not can_coin:
                    continue

                mdd_result = mdd.update(balance)
                if not mdd_result["allowed"]:
                    logger.warning("MDD 한도: %s", mdd_result["reason"])
                    continue

                # 최대 포지션 수
                if len(pos_mgr.positions) >= CONFIG.MAX_OPEN_POSITIONS:
                    continue

                # 동시보유 총리스크 체크 (실제 R 기반)
                r_1_usd = balance * CONFIG.RISK_PER_TRADE_PCT / 100
                total_risk_r = 0.0
                for p in pos_mgr.positions.values():
                    pos_risk_usd = (
                        p.total_size * p.remaining_ratio
                        * p.initial_r_distance / p.avg_price
                    ) if p.avg_price > 0 else 0
                    total_risk_r += pos_risk_usd / r_1_usd if r_1_usd > 0 else 0
                if total_risk_r >= CONFIG.MAX_TOTAL_RISK_R:
                    continue

                # 이미 포지션 있으면 스킵
                has_pos = any(
                    p.symbol == symbol for p in pos_mgr.positions.values()
                )
                if has_pos:
                    continue

                # ── 6. BTC 필터 (ETH일 때) ──
                # ── 7. 신규 진입 ──
                signals = generate_signals(
                    symbol, df, regime, last_entry_candle.get(symbol),
                )
                if signals:
                    sig = signals[0]

                    if "ETH" in symbol:
                        if btc_df is None:
                            pos_mgr.blocked.register_blocked(
                                symbol, sig.side, sig.entry_price,
                                sig.sl_price, sig.tp_price,
                                "BTC데이터없음→ETH차단", bar_idx,
                            )
                            continue
                        from fang_v10.btc_filter import can_enter_eth
                        can_eth, eth_reason = can_enter_eth(btc_df, sig.side)
                        if not can_eth:
                            pos_mgr.blocked.register_blocked(
                                symbol, sig.side, sig.entry_price,
                                sig.sl_price, sig.tp_price,
                                f"BTC필터:{eth_reason}", bar_idx,
                            )
                            continue

                    sizing = calc_position_size(
                        balance, sig.entry_price,
                        sig.sl_price, sig.tp_price, symbol,
                    )

                    if sizing.valid:
                        result = executor.safe_open(symbol, sig.side, sizing)
                        if result:
                            fill = result["fill_price"]
                            atr = df.iloc[-1].get("atr", 0)
                            pos_mgr.open_position(
                                symbol, sig.side, fill, sizing.amount,
                                sizing.leverage, atr, bar_idx,
                                regime.value, sig.strategy,
                            )
                            last_entry_candle[symbol] = bar_idx
                    else:
                        pos_mgr.blocked.register_blocked(
                            symbol, sig.side, sig.entry_price,
                            sig.sl_price, sig.tp_price,
                            f"sizing:{sizing.reject_reason}", bar_idx,
                        )

            # ── 8. 상태 저장 ──
            state.save(
                {k: vars(v) for k, v in pos_mgr.positions.items()},
                risk_eng.get_state(),
                mdd.get_state(),
            )

            loop_count += 1

            # 60초마다 경량 reconcile
            if loop_count % 12 == 0 and not CONFIG.PAPER_TRADING:
                exchange_positions = []
                for sym in CONFIG.SYMBOLS:
                    pos = client.get_position(sym)
                    if pos:
                        exchange_positions.append(pos)
                local_pos = {k: vars(v) for k, v in pos_mgr.positions.items()}
                state.reconcile(local_pos, exchange_positions, client, risk_eng)

            # 5분(60루프)마다 상태 로그 + phantom/blocked 통계
            if loop_count % 60 == 0:
                regimes = {}
                for sym in CONFIG.SYMBOLS:
                    regimes[sym] = regime_eng.detect(sym, df, len(df) - 1).value
                log_state(balance, pos_mgr.positions, 0, regimes, risk_eng.get_risk_mode())
                log_phantom_stats(pos_mgr.phantom.get_stats())
                log_blocked_stats(pos_mgr.blocked.get_stats())

            await asyncio.sleep(CONFIG.MAIN_LOOP_SEC)

        except KeyboardInterrupt:
            logger.info("사용자 중단")
            break
        except Exception as e:
            logger.critical("메인루프 예외: %s", e, exc_info=True)
            await asyncio.sleep(30)

    # 종료 시 상태 저장
    state.save(
        {k: vars(v) for k, v in pos_mgr.positions.items()},
        risk_eng.get_risk_mode(),
        mdd.update(balance),
    )
    logger.info("fang_v10 종료")


if __name__ == "__main__":
    asyncio.run(main_loop())
