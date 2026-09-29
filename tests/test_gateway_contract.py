"""The gateway contract, and what it costs to satisfy it.

The point of this file is one property: a gateway is *not* required to be a thread.
That was not true until recently -- `ExchangeGateway` extended `EventLoop`, so every
gateway carried a queue, and a backtest had to hand the simulator a fake queue to
get a fill computed inside the strategy's own call.
"""

import pytest

from qate.core.ev_loop import EventLoop
from qate.core.gateway import ExchangeGateway
from qate.core.model import ExchangeName, Market, OrderBook, OrderLevel, SettleType, Side
from qate.core.order import OrderRequest, OrderType
from qate.core.symbol import Symbol
from qate.simulator import SimulatorGateway
from qate.trading.gateways import DefaultGateway, QueuedGateway

MARKET = Market(ExchangeName.GMO, Symbol.BTC_JPY)
TS = 1767225600.0


def book(mid: float) -> OrderBook:
    return OrderBook(
        [OrderLevel(mid - 500, 1.0)],
        [OrderLevel(mid + 500, 1.0)],
        MARKET.symbol,
        MARKET.exchange_name,
        TS,
    )


def request(side: Side = Side.BUY) -> OrderRequest:
    return OrderRequest(
        ts=TS,
        market=MARKET,
        side=side,
        price=15_000_000.0,
        size=0.01,
        settle_type=SettleType.OPEN,
        order_type=OrderType.DEFAULT,
    )


def test_the_contract_is_abstract():
    with pytest.raises(TypeError):
        ExchangeGateway()


def test_the_simulator_satisfies_the_contract_without_being_a_thread():
    """The property the split bought. A replay needs no queue of its own."""
    gateway = SimulatorGateway(ExchangeName.GMO)
    assert isinstance(gateway, ExchangeGateway)
    assert not isinstance(gateway, EventLoop)
    assert not hasattr(gateway, "event_queue")


def test_a_queued_gateway_is_both():
    """The live path: a gateway that must not block the strategy runs its own loop."""
    assert issubclass(QueuedGateway, ExchangeGateway)
    assert issubclass(QueuedGateway, EventLoop)
    assert issubclass(DefaultGateway, QueuedGateway)


def test_the_simulator_answers_inside_the_call():
    """`create` publishes ORDER_CREATED before returning -- no loop to drain."""
    gateway = SimulatorGateway(ExchangeName.GMO)
    seen: list = []
    gateway.add_order_listener(_Collector(seen))

    gateway.create(request())
    assert [e[0] for e in seen] == ["ORDER_CREATED"]


def test_a_fill_comes_from_the_next_book_not_the_call():
    gateway = SimulatorGateway(ExchangeName.GMO, slippage_rate=0.0)
    seen: list = []
    gateway.add_order_listener(_Collector(seen))

    gateway.create(request())
    assert "ORDER_FILLED" not in [e[0] for e in seen]

    gateway.handle_order_book(book(15_000_000.0))
    assert "ORDER_FILLED" in [e[0] for e in seen]
    filled = [e[1] for e in seen if e[0] == "ORDER_FILLED"][0]
    assert filled.exec_price == 15_000_500.0  # the ask of the book that matched it


def test_a_live_feed_can_still_drive_the_simulator():
    """Paper trading: books arrive from a feed rather than from a replay driver."""
    from qate.core.feed import MarketDataFeed

    gateway = SimulatorGateway(ExchangeName.GMO, slippage_rate=0.0)
    seen: list = []
    gateway.add_order_listener(_Collector(seen))

    feed = MarketDataFeed()
    gateway.subscribe_market_data_feed(feed)

    gateway.create(request())
    feed.publish_order_book(book(15_000_000.0))
    assert "ORDER_FILLED" in [e[0] for e in seen]


class _Collector:
    def __init__(self, events: list):
        self.events = events

    def put(self, event):
        if event:
            self.events.append(event)
