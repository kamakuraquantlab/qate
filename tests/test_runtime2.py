import pytest

from qate.core.ev_type import EventType
from qate.core.feed import OrderFeed
from qate.core.gateway import ExchangeGateway
from qate.core.model import ExchangeName, Market, SettleType, Side
from qate.core.order import OrderRequest, OrderResponse, OrderState
from qate.core.symbol import Symbol
from qate.trading.runtime2 import Runtime2, StrategyEvent, _Gateway, _StatusSink


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
    a, b = _Gateway(gateway, runtime, "a"), _Gateway(gateway, runtime, "b")
    runtime._instance_gateways[("a", gateway.exchange_name)] = a
    runtime._instance_gateways[("b", gateway.exchange_name)] = b
    return runtime, gateway, a, b


def test_runtime_submits_then_routes_a_response_only_to_the_owning_trader():
    runtime, gateway, a, b = proxies()
    a_events, b_events = Events(), Events()
    a.add_order_listener(a_events)
    b.add_order_listener(b_events)

    order_request = request()
    a.create(order_request)
    gateway.publish_order_created(OrderResponse(order_request, state=OrderState.CREATED))
    runtime._next()

    assert gateway.created == [order_request]
    assert len(a_events.items) == 1
    assert b_events.items == []


def test_terminal_response_needs_no_order_lifetime_tracking():
    runtime, gateway, proxy, _ = proxies()
    events = Events()
    proxy.add_order_listener(events)
    order_request = request()
    proxy.create(order_request)

    gateway.publish_order_filled(OrderResponse(order_request, state=OrderState.FILLED))
    runtime._next()

    assert len(events.items) == 1
    assert order_request.instance_key == "a"
    assert not hasattr(runtime, "_order_owners")


def test_runtime_rejects_cancelling_another_traders_order():
    _, _, a, b = proxies()
    order_request = request()
    a.create(order_request)

    with pytest.raises(ValueError, match="Trader cannot cancel"):
        b.cancel(order_request)


def test_proxy_delegates_adapter_specific_capabilities():
    _, _, proxy, _ = proxies()

    assert proxy.fetch_balance_sync(Symbol.BTC_JPY) == Symbol.BTC_JPY


def test_order_updates_are_routed_without_releasing_ownership():
    runtime, gateway, proxy, _ = proxies()
    events = Events()
    proxy.add_order_listener(events)
    order_request = request()
    proxy.create(order_request)

    gateway.publish_order_updated(OrderResponse(order_request, state=OrderState.CREATED))
    runtime._next()

    assert events.items[0][0] == EventType.ORDER_UPDATED
    assert order_request.instance_key == "a"


def test_a_filled_order_is_reported_with_its_instance_key():
    runtime, _, _, _ = proxies()
    reported = []
    runtime.register(EventType.TRADING_ORDER, reported.append)
    order_request = request()
    response = OrderResponse(order_request, state=OrderState.FILLED)
    _StatusSink(runtime, "a").put((EventType.TRADING_ORDER, response))
    runtime._next()

    assert reported == [StrategyEvent("a", response)]


def test_an_order_request_cannot_be_reassigned_to_another_instance():
    _, _, a, b = proxies()
    order_request = request()
    a.create(order_request)

    with pytest.raises(ValueError, match="already belongs"):
        b.create(order_request)


def test_duplicate_strategy_instance_keys_are_rejected():
    runtime = Runtime2()
    runtime.add_strategy(type("S", (), {})(), "same")

    with pytest.raises(ValueError, match="Duplicate strategy instance key"):
        runtime.add_strategy(type("S", (), {})(), "same")
