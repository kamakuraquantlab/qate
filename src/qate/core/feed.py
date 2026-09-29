"""Observer mixins: who is listening for what.

Four sets of listener lists, mixed into the classes that publish. A gateway is an
`OrderFeed`, a websocket connection is a `MarketDataFeed`, anything that reports its
own health is a `StatusFeed`.

## Each one initialises only its own state

No `super().__init__()` here, deliberately, and a class that mixes one in must call
its `__init__` explicitly.

These are mixed in beside `threading.Thread`, which does not play the cooperative
game: it takes its own arguments and is initialised by name. So a chain of
`super().__init__()` through the mixins had to be *entered* by an explicit
`EventLoop.__init__(self, queue, heartbeat)` call -- and any second explicit call in
the same constructor re-entered the whole chain from wherever it landed in the MRO.
`QueuedGateway` did exactly that, and initialised `EventLoop`, `StatusFeed` and
`Thread` twice per gateway: a queue built and discarded, a handler table cleared
and refilled, a Thread's internals re-established.

Explicit initialisation is longer to write and says what happens. The cost is that
a class combining mixins must remember them all, which
`tests/test_initialisation.py` checks for every class in the library.
"""

from .ev_q import EventQueue
from .ev_type import EventType
from .model import OrderBook, Symbol, Trade
from .order import ExchangeExecution, ExchangeOrder, OrderResponse


class StatusFeed:
    def __init__(self):
        self.status_listeners: list[EventQueue] = []

    def add_status_listener(self, listener: EventQueue):
        self.status_listeners.append(listener)

    def publish_status(self, event_type, event):
        for listener in self.status_listeners:
            listener.put((event_type, event))

    def publish_last(self):
        for listener in self.status_listeners:
            listener.put(None)


class OrderFeed:
    def __init__(self):
        self.order_listeners: list[EventQueue] = []

    def add_order_listener(self, listener: EventQueue):
        self.order_listeners.append(listener)

    def publish_order_created(self, order_response: OrderResponse):
        for listener in self.order_listeners:
            listener.put((EventType.ORDER_CREATED, order_response))

    def publish_order_updated(self, order_response: OrderResponse):
        for listener in self.order_listeners:
            listener.put((EventType.ORDER_UPDATED, order_response))

    def publish_order_filled(self, order_response: OrderResponse):
        for listener in self.order_listeners:
            listener.put((EventType.ORDER_FILLED, order_response))

    def publish_order_cancelled(self, order_response: OrderResponse):
        for listener in self.order_listeners:
            listener.put((EventType.ORDER_CANCELLED, order_response))

    def publish_order_error(self, order_response: OrderResponse):
        for listener in self.order_listeners:
            listener.put((EventType.ORDER_ERROR, order_response))


class MarketDataFeed:
    def __init__(self):
        self.order_book_listeners: list[EventQueue] = []
        self.trade_listeners: list[EventQueue] = []
        self.subscribe_event_types: dict[Symbol, set[str]] = {}

    def publish_order_book(self, order_book: OrderBook):
        for listener in self.order_book_listeners:
            listener.put((EventType.MARKET_ORDER_BOOK, order_book))

    def add_order_book_listener(self, listener: EventQueue):
        self.order_book_listeners.append(listener)

    def publish_trade(self, trade: Trade):
        for listener in self.trade_listeners:
            listener.put((EventType.MARKET_TRADE, trade))

    def add_trade_listener(self, listener: EventQueue):
        self.trade_listeners.append(listener)

    def register_symbol_event(self, symbol: Symbol, event_type: str):
        if symbol not in self.subscribe_event_types:
            self.subscribe_event_types[symbol] = set()
        self.subscribe_event_types[symbol].add(event_type)


class ExchangeFeed:
    def __init__(self):
        self.exchange_execution_listeners: list[EventQueue] = []
        self.exchange_order_listeners: list[EventQueue] = []

    def publish_execution(self, execution: ExchangeExecution):
        for listener in self.exchange_execution_listeners:
            listener.put((EventType.EXCHANGE_EXECUTION, execution))

    def add_execution_listener(self, listener: EventQueue):
        self.exchange_execution_listeners.append(listener)

    def publish_order(self, exchange_order: ExchangeOrder):
        for listener in self.exchange_order_listeners:
            listener.put((EventType.EXCHANGE_ORDER, exchange_order))

    def add_order_listener(self, listener: EventQueue):
        self.exchange_order_listeners.append(listener)
