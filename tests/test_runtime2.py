import pytest

from qate.core.ev_type import EventType
from qate.core.feed import OrderFeed
from qate.core.gateway import ExchangeGateway
from qate.core.model import ExchangeName, Market, SettleType, Side
from qate.core.order import OrderRequest, OrderResponse, OrderState
from qate.core.symbol import Symbol
from qate.trading.runtime2 import Runtime2, StrategyEvent, _Gateway


class Gateway(ExchangeGateway):
    def __init__(self):
        OrderFeed.__init__(self)
        self.created = []
        self.cancelled = []

    @property
    def exchange_name(self):
        return ExchangeName.GMO

    def create(self, order_request):
        self.created.append(order_request)

    def cancel(self, order_request):
        self.cancelled.append(order_request)

    def fetch_balance_sync(self, symbol):
        return symbol


class Events:
    def __init__(self):
        self.items = []

    def put(self, item):
        self.items.append(item)


def request():
    return OrderRequest(
        1.0,
        Market(ExchangeName.GMO, Symbol.BTC_JPY),
        Side.BUY,
        100.0,
        1.0,
        SettleType.OPEN,
    )


def proxies():
    runtime = Runtime2()
    gateway = Gateway()
    gateway.add_order_listener(runtime.event_queue)
    runtime.setup_multi()
    return runtime, gateway, _Gateway(gateway, runtime, "a"), _Gateway(gateway, runtime, "b")


def test_runtime_submits_then_routes_a_response_only_to_the_owning_trader():
    runtime, gateway, a, b = proxies()
    a_events, b_events = Events(), Events()
    a.add_order_listener(a_events)
    b.add_order_listener(b_events)

    order_request = request()
    a.create(order_request)
    runtime._next()
    gateway.publish_order_created(OrderResponse(order_request, state=OrderState.CREATED))
    runtime._next()

    assert gateway.created == [order_request]
    assert len(a_events.items) == 1
    assert b_events.items == []


def test_terminal_response_releases_order_ownership():
    runtime, gateway, proxy, _ = proxies()
    events = Events()
    proxy.add_order_listener(events)
    order_request = request()
    proxy.create(order_request)
    runtime._next()

    gateway.publish_order_filled(OrderResponse(order_request, state=OrderState.FILLED))
    runtime._next()

    assert len(events.items) == 1
    assert id(order_request) not in runtime._order_owners


def test_runtime_rejects_cancelling_another_traders_order():
    runtime, _, a, b = proxies()
    order_request = request()
    runtime.handle_create_order((a, order_request))

    with pytest.raises(ValueError, match="Trader cannot cancel"):
        runtime.handle_cancel_order((b, order_request))


def test_proxy_delegates_adapter_specific_capabilities():
    _, _, proxy, _ = proxies()

    assert proxy.fetch_balance_sync(Symbol.BTC_JPY) == Symbol.BTC_JPY


def test_order_updates_are_routed_without_releasing_ownership():
    runtime, gateway, proxy, _ = proxies()
    events = Events()
    proxy.add_order_listener(events)
    order_request = request()
    proxy.create(order_request)
    runtime._next()

    gateway.publish_order_updated(OrderResponse(order_request, state=OrderState.CREATED))
    runtime._next()

    assert events.items[0][0] == EventType.ORDER_UPDATED
    assert runtime._order_owners[id(order_request)] is proxy


def test_a_filled_order_is_reported_with_its_instance_key():
    runtime, gateway, proxy, _ = proxies()
    reported = []
    runtime.register("RUNTIME_INSTANCE_ORDER_FILLED", reported.append)
    order_request = request()
    runtime.handle_create_order((proxy, order_request))

    response = OrderResponse(order_request, state=OrderState.FILLED)
    runtime.handle_order_response(EventType.ORDER_FILLED, response)
    runtime._next()

    assert reported == [StrategyEvent("a", response)]


def test_duplicate_strategy_instance_keys_are_rejected():
    runtime = Runtime2()
    runtime.add_strategy(type("S", (), {})(), "same")

    with pytest.raises(ValueError, match="Duplicate strategy instance key"):
        runtime.add_strategy(type("S", (), {})(), "same")
