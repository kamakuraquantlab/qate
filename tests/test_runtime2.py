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


def test_a_quitting_strategy_does_not_strand_another_strategys_cancel():
    """One trader exits; the others must still get their shutdown cancels confirmed.

    The shutdown used to block this runtime's own loop while it joined traders, so
    the confirmation a stopping strategy was waiting for was never routed to it.
    """
    import threading

    from qate.core.ev_type import EventType
    from qate.core.model import EventLoopExit, OrderBook, OrderLevel
    from qate.trading.gateways import SimulatorGateway
    from qate.trading.strategy import Strategy

    book = OrderBook([OrderLevel(99.0, 1.0)], [OrderLevel(101.0, 1.0)], Symbol.BTC_JPY, ExchangeName.GMO, 1.0)

    class CancelsOnStop(Strategy):
        is_ready = True

        def __init__(self):
            super().__init__()
            self.created = threading.Event()
            self.cancelled = threading.Event()

        def add_gateway(self, gateway):
            self.gateway = gateway

        def before_loop(self):
            self.gateway.create(request())

        def handle_order_created(self, order_response):
            self.order = order_response
            self.created.set()

        def handle_stop(self, _):
            order_request = self.order.order_request
            order_request.order_id = self.order.order_id
            self.gateway.cancel(order_request)

        def handle_order_cancelled(self, order_response):
            self.cancelled.set()
            raise EventLoopExit()

    class Quits(Strategy):
        is_ready = True

        def handle_order_book(self, order_book):
            raise EventLoopExit()

    runtime = Runtime2()
    gateway = SimulatorGateway(ExchangeName.GMO)
    runtime.add_gateway(gateway)
    canceller = CancelsOnStop()
    runtime.add_strategy(canceller, "a")
    runtime.add_strategy(Quits(), "b")
    for trader in runtime.traders:
        trader.event_queue.put((EventType.MARKET_ORDER_BOOK, book))  # consumed by warmup

    runtime.start()
    try:
        assert canceller.created.wait(2)
        runtime.traders[1].event_queue.put((EventType.MARKET_ORDER_BOOK, book))  # "b" quits
        runtime.join(timeout=4)
        assert not runtime.is_alive()
        assert canceller.cancelled.is_set()
        assert not gateway.is_alive()
    finally:
        runtime.stop()
        runtime.join(timeout=10)
