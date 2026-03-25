"""드라이런 3분 — PAPER 모드, 네트워크 필요."""
import asyncio
import time

from fang_v10.config import CONFIG
CONFIG.PAPER_TRADING = True

from fang_v10.exchange_api import BitgetClient
from fang_v10.regime_engine import RegimeEngine, ensure_indicators
from fang_v10.position_manager import PositionManager
from fang_v10.risk_engine import RiskEngine
from fang_v10.strategy import generate_signals
from fang_v10.monitor import setup_logging


async def dry_run():
    setup_logging()
    client = BitgetClient("", "", "", paper=True)
    regime_eng = RegimeEngine()
    pos_mgr = PositionManager()
    risk_eng = RiskEngine()
    risk_eng.set_initial_balance(198.0)

    start = time.time()
    cycle = 0

    while time.time() - start < 180:
        cycle += 1
        print(f"\n--- 사이클 {cycle} ---")

        for symbol in CONFIG.SYMBOLS:
            try:
                df = client.fetch_ohlcv(symbol, "5m", 200)
                if df.empty:
                    print(f"  {symbol}: 캔들 없음")
                    continue

                df = ensure_indicators(df)
                bar_idx = len(df) - 1
                price = float(df.iloc[-1]["close"])
                regime = regime_eng.detect(symbol, df, bar_idx)
                direction = regime_eng.get_trend_direction(symbol, df)
                print(f"  {symbol}: {regime.value} ({direction}) ${price:.2f}")

                signals = generate_signals(symbol, df, regime, -1)
                if signals:
                    for sig in signals:
                        print(f"    신호: {sig.side} {sig.strategy} "
                              f"SL={sig.sl_price:.2f} TP={sig.tp_price:.2f}")
                        print(f"    사유: {sig.reason}")
            except Exception as e:
                print(f"  {symbol} 에러: {e}")

        rm = risk_eng.get_risk_mode()
        print(f"  리스크: {rm['mode']} mult={rm['size_mult']}")
        await asyncio.sleep(CONFIG.MAIN_LOOP_SEC)

    print(f"\n🎉 드라이런 완료! {cycle}사이클")


if __name__ == "__main__":
    asyncio.run(dry_run())
