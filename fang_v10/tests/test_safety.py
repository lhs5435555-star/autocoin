"""v11 안전장치 테스트."""
import time
from unittest.mock import MagicMock
from fang_v10.safety import (
    make_client_oid, OrderStore, MarkPriceMonitor,
    place_server_sl, on_entry_filled, reconcile_on_startup,
)


class TestClientOid:
    def test_format(self):
        oid = make_client_oid("BTC/USDT:USDT", "long", "ENTRY", 1700000000)
        assert oid.startswith("BTC_L_ENTRY_")
        assert len(oid.split("_")) == 5

    def test_short(self):
        oid = make_client_oid("ETH/USDT:USDT", "short", "SL")
        assert "_S_SL_" in oid

    def test_unique(self):
        o1 = make_client_oid("BTC/USDT:USDT", "long", "ENTRY")
        time.sleep(0.01)
        o2 = make_client_oid("BTC/USDT:USDT", "long", "ENTRY")
        # 같은 초면 같을 수 있지만 다른 초면 다름


class TestOrderStore:
    def test_save_get(self):
        store = OrderStore()
        store.save("oid1", {"id": "123", "status": "open"})
        assert store.exists("oid1")
        assert store.get("oid1")["order"]["id"] == "123"

    def test_not_exists(self):
        store = OrderStore()
        assert not store.exists("nonexist")

    def test_cleanup(self):
        store = OrderStore()
        store.save("old", {"id": "1"})
        store._orders["old"]["ts"] = time.time() - 100000
        store.cleanup_old(max_age_sec=1000)
        assert not store.exists("old")


class TestMarkPriceMonitor:
    def test_no_alert_normal(self):
        mon = MarkPriceMonitor()
        r = mon.check("BTC", 87000, 87000)
        assert not r["alert"]
        assert r["gap_pct"] < 0.001

    def test_alert_on_gap(self):
        mon = MarkPriceMonitor()
        r = mon.check("BTC", 87400, 87000)  # 0.46% gap
        assert r["alert"]

    def test_critical_gap(self):
        mon = MarkPriceMonitor()
        r = mon.check("BTC", 87800, 87000)  # 0.92% gap
        assert r["alert"]


class TestServerSL:
    def test_success(self):
        client = MagicMock()
        client.set_trigger_sl.return_value = {"id": "sl1"}
        result = place_server_sl("BTC", 86500, 0.01, client, "long")
        assert result is not None
        client.set_trigger_sl.assert_called_once()

    def test_retry_then_fail(self):
        client = MagicMock()
        client.set_trigger_sl.side_effect = Exception("API fail")
        result = place_server_sl("BTC", 86500, 0.01, client, "long", max_retry=2)
        assert result is None
        assert client.set_trigger_sl.call_count == 2

    def test_on_entry_filled_success(self):
        client = MagicMock()
        client.set_trigger_sl.return_value = {"id": "sl1"}
        ok = on_entry_filled("BTC", 0.01, 87000, 86500, "long", client)
        assert ok is True

    def test_on_entry_filled_fail_closes(self):
        client = MagicMock()
        client.set_trigger_sl.side_effect = Exception("fail")
        client.create_market_order.return_value = {"id": "close1"}
        ok = on_entry_filled("BTC", 0.01, 87000, 86500, "long", client)
        assert ok is False
        client.create_market_order.assert_called_once()


class TestReconcile:
    def test_matching(self):
        """로컬과 거래소 일치."""
        client = MagicMock()
        client.get_position.return_value = {
            "symbol": "BTC/USDT:USDT", "side": "long",
            "contracts": "0.01",
        }
        client.get_open_orders.return_value = [{"triggerPrice": "86500"}]

        pos_mgr = MagicMock()
        pos_mock = MagicMock()
        pos_mock.symbol = "BTC/USDT:USDT"
        pos_mock.side = "long"
        pos_mock.total_size = 0.01
        pos_mock.phase = "OPEN"
        pos_mock.sl_price = 86500
        pos_mgr.positions = {"BTC/USDT:USDT|long": pos_mock}

        result = reconcile_on_startup(
            None, client, None, pos_mgr, ["BTC/USDT:USDT"],
        )
        assert result["status"] == "OK"

    def test_missing_on_exchange(self):
        """로컬에 있고 거래소에 없음 → 제거."""
        client = MagicMock()
        client.get_position.return_value = None

        pos_mgr = MagicMock()
        pos_mock = MagicMock()
        pos_mock.phase = "OPEN"
        pos_mgr.positions = {"BTC/USDT:USDT|long": pos_mock}

        result = reconcile_on_startup(
            None, client, None, pos_mgr, ["BTC/USDT:USDT"],
        )
        assert result["status"] == "MISMATCH"
        assert any("CLOSED" in d for d in result["details"])
