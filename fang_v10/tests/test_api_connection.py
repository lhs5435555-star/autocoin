"""API 연결 테스트 — .env에 키 설정 후 실행."""
from fang_v10.config import CONFIG
from fang_v10.exchange_api import BitgetClient


def test_api():
    client = BitgetClient(
        CONFIG.API_KEY, CONFIG.API_SECRET,
        CONFIG.PASSPHRASE, paper=False,
    )
    print("=" * 50)
    balance = client.fetch_balance()
    print(f"✅ 잔고: ${balance:.2f}")
    for sym in ["BTC/USDT:USDT", "ETH/USDT:USDT"]:
        pos = client.get_position(sym)
        print(f"✅ {sym} 포지션: {pos}")
        df = client.fetch_ohlcv(sym, "5m", 10)
        print(f"✅ {sym} 캔들: {len(df)}봉, close={df.iloc[-1]['close']}")
        info = client.get_market_info(sym)
        print(f"✅ {sym} 정밀도: {info}")
        lev = client.ensure_ready(sym, 10)
        print(f"✅ {sym} 레버리지: {lev}x")
    print("=" * 50)
    print("🎉 API 전체 통과!")


if __name__ == "__main__":
    test_api()
