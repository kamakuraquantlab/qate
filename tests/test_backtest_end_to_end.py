"""A whole backtest, through the public package only.

Order books in, replayed through the simulator, a metric log on disk. This is the
path the library exists to support, and it is worth one test that walks all of it
rather than several that each mock the next piece.

It stops at the metric log, because that is where `qate` stops. Turning a run into
parquet results is the backtester's job and is tested there.

The books are built in memory rather than read from a file. `qate` does not know
any storage layout -- reading recorded data belongs to whoever owns it -- so a
test that needed parquet would be testing a dependency this package does not
have. Events in, events out.

Nothing here installs an exchange adapter, and the second test proves the backtest
path never even asks for one.
"""

from qate.core.ev_type import EventType
from qate.core.model import ExchangeName, Market, Measurement, OrderBook, OrderLevel, SettleType, Side
from qate.core.order import OrderRequest, OrderType
from qate.core.symbol import Symbol
from qate.simulator import ReplayQueue, SimulatorGateway
from qate.trading import metric_log
from qate.trading.strategy import Strategy
from qate.trading.trader import Trader
from qate.util.dt_range import DtRange

MARKET = Market(ExchangeName.GMO, Symbol.BTC_JPY)
DEPTH = 5
START_TS = DtRange.from_strings("20260115", "20260115").start_ts


def make_books(prices: list[float]) -> list[tuple[str, OrderBook]]:
    """One book per price, a fixed 1000-wide spread, 1.0 resting at each level."""
    events = []
    for n, mid in enumerate(prices):
        bids = [OrderLevel(mid - 500 - i * 100, 1.0) for i in range(DEPTH)]
        asks = [OrderLevel(mid + 500 + i * 100, 1.0) for i in range(DEPTH)]
        events.append(
            (
                EventType.MARKET_ORDER_BOOK,
                OrderBook(bids, asks, MARKET.symbol, MARKET.exchange_name, START_TS + n),
            )
        )
    return events


class BuyThenSell(Strategy):
    """Buys on the first book it handles, sells once that buy has filled.

    Deliberately minimal: what is being tested is that the wiring carries an
    order from a strategy to a fill and back, not a trading idea.
    """

    ORDER_SIZE = 0.01

    def __init__(self):
        super().__init__(params={"size": self.ORDER_SIZE})
        self._is_ready = True
        self.gateway = None
        self.warmup_books = 0
        self.books_seen = 0
        self.fills: list = []
        self._next_side = Side.BUY
        self._in_flight = False

    def add_gateway(self, gateway):
        self.gateway = gateway

    def warmup_order_book(self, order_book):
        self.warmup_books += 1

    def handle_order_book(self, order_book):
        self.books_seen += 1
        self.add_metric(order_book.market_price(self.ORDER_SIZE).to_metric_object())

        if self._in_flight or self._next_side is None:
            return

        self._in_flight = True
        # OrderType.DEFAULT is the taker path in the simulator: it crosses the
        # spread at the touch rather than resting.
        self.gateway.create(
            OrderRequest(
                ts=order_book.get_ts(),
                market=MARKET,
                side=self._next_side,
                price=order_book.mid,
                size=self.ORDER_SIZE,
                settle_type=SettleType.OPEN if self._next_side == Side.BUY else SettleType.CLOSE,
                order_type=OrderType.DEFAULT,
            )
        )

    def handle_order_filled(self, order_response):
        super().handle_order_filled(order_response)
        self.fills.append(order_response)
        self._in_flight = False
        self._next_side = Side.SELL if self._next_side == Side.BUY else None


# Six books, the mid rising by 10,000 each. Six because an order created on one
# book fills on the next, so a buy and a sell need four, plus the one Warmup
# consumes and one after the last fill.
MIDS = [15_000_000.0 + n * 10_000 for n in range(6)]


def run_backtest(tmp_path) -> BuyThenSell:
    """The whole path: recorded books, through the simulator, into a metric log.

    Returns the finished strategy, for the caller to assert on.
    """
    events = make_books(MIDS)
    assert len(events) == len(MIDS)
    assert events[0][0] == EventType.MARKET_ORDER_BOOK

    # A strategy, a simulator standing in for GMO, and one thread.
    strategy = BuyThenSell()
    gateway = SimulatorGateway(ExchangeName.GMO, slippage_rate=0.0)

    trader = Trader(strategy, ReplayQueue(events))
    trader.add_gateway(gateway)
    trader.register(EventType.MARKET_ORDER_BOOK, gateway.handle_order_book)

    metrics_writer = metric_log.MetricLog(metric_log.RotationInterval.ONE_DAY, 2, "Metrics")

    collected: list = []
    trader.add_status_listener(_Collector(collected, metrics_writer))

    trader.run()

    metrics_writer.close()
    return strategy


def test_backtest_fills_orders_and_writes_a_metric_log(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    strategy = run_backtest(tmp_path)

    # Every book was accounted for, and the replay ended when the data did with
    # no sentinel appended. Trader.before_loop runs a Warmup that drains the
    # queue until the strategy declares itself ready, which costs one event even
    # for a strategy that is ready from the start.
    assert strategy.warmup_books == 1
    assert strategy.books_seen == len(MIDS) - 1

    # A buy and a sell, each filled at the touch of the book *after* the one it
    # was created on. That one-book delay is the simulator's latency model: an
    # order is matched by the next snapshot, never by the one the strategy was
    # looking at when it decided.
    assert [f.order_request.side for f in strategy.fills] == [Side.BUY, Side.SELL]
    buy, sell = strategy.fills
    assert buy.exec_price == MIDS[2] + 500  # third book's best ask
    assert sell.exec_price == MIDS[4] - 500  # fifth book's best bid
    assert buy.exec_size == sell.exec_size == BuyThenSell.ORDER_SIZE

    # The log on disk, readable back with no database anywhere in sight.
    written = list(metric_log.read_metrics_dir(tmp_path, "Metrics"))
    assert len(written) == strategy.books_seen
    assert {m[0] for m in written} == {Measurement.MARKET_PRICE.value}


def test_cancelling_an_already_filled_order_is_not_a_fault():
    """The fill/cancel race a real strategy hits, and a venue tolerates.

    An order can be matched against a book and the strategy can decide, from that
    same book, to cancel it -- before the fill it caused has been delivered. The
    simulator used to raise here, which surfaced as a logged traceback in every
    run of a strategy that reprices. A venue answers "no such order"; so does this.

    It must not publish an order error either: `Strategy` treats a cancel error as
    grounds to stop trading, which would turn an unnecessary cancel into a halt.
    """
    gateway = SimulatorGateway(ExchangeName.GMO, slippage_rate=0.0)

    errors: list = []
    gateway.add_order_listener(_ErrorCollector(errors))

    request = OrderRequest(
        ts=START_TS,
        market=MARKET,
        side=Side.BUY,
        price=MIDS[0],
        size=0.01,
        settle_type=SettleType.OPEN,
        order_type=OrderType.DEFAULT,
    )
    gateway.create(request)
    book = make_books([MIDS[0]])[0][1]
    gateway.handle_order_book(book)          # fills, and forgets the order
    assert request.ctx_id not in gateway.orders

    gateway.cancel(request)                  # the race: must not raise
    assert not [e for e in errors if e[0] == EventType.ORDER_ERROR]


class _ErrorCollector:
    def __init__(self, events: list):
        self.events = events

    def put(self, data):
        if data is not None:
            self.events.append(data)


def test_a_backtest_never_asks_for_an_exchange(tmp_path, monkeypatch):
    """The publication guarantee, asserted where it matters.

    Not "no adapter is installed" -- that depends on the machine, and on a
    developer's machine an adapter usually is. What must hold everywhere is that
    the backtest path never reaches the registry at all, so installing an adapter
    cannot change what a replay does. A tripwire in place of every registry entry
    point proves it: the run completes without tripping one.
    """
    from qate.exchange import registry

    def tripwire(*args, **kwargs):
        raise AssertionError("a backtest asked the exchange registry for a venue")

    monkeypatch.setattr(registry, "get", tripwire)
    monkeypatch.setattr(registry, "discover", tripwire)
    monkeypatch.setattr(registry, "registered", tripwire)
    monkeypatch.chdir(tmp_path)

    strategy = run_backtest(tmp_path)
    assert len(strategy.fills) == 2


class _Collector:
    """Stands in for a result queue: metrics to the local writer, orders kept."""

    def __init__(self, orders: list, metrics_writer):
        self.orders = orders
        self.metrics_writer = metrics_writer

    def put(self, data):
        if data is None:
            return
        (event_type, event) = data
        if event_type == EventType.METRICS:
            for metric_object in event:
                self.metrics_writer.add(metric_log.MetricRecord(metric_object[1], metric_object))
        elif event_type == EventType.ORDER_FILLED:
            self.orders.append(event)
