import json
from logging import getLogger

from qate.core.ev_loop import EventLoop
from qate.core.ev_q import EventQueue
from qate.core.ev_type import EventType
from qate.core.feed import MarketDataFeed
from qate.core.gateway import ExchangeGateway
from qate.core.model import EventLoopExit

from .strategy import Strategy

LOG = getLogger(__name__)


class Trader(EventLoop):
    """Drives one strategy: warmup, then market and order events into its handlers.

    `reload_params` re-reads `params.json` every 10 seconds and hands it to
    `Strategy.update_params`, which is how a live run takes a parameter change
    without a restart. It is on by default because a live run is the default.
    A replay turns it off. A backtest sets each run's parameters itself, and
    the task's clock is the wall clock, so a sweep would have every run's
    parameters replaced by the file ten real seconds in -- at an arbitrary
    point in the replay, which is worse than at the start.
    """

    def __init__(self, strategy: Strategy, event_queue: EventQueue = None, reload_params: bool = True):
        super(Trader, self).__init__(event_queue)
        self.strategy = strategy
        self.has_gateway = False
        self.reload_params = reload_params

    def subscribe_market_data_feed(self, market_feed: MarketDataFeed):
        market_feed.add_order_book_listener(self.event_queue)
        market_feed.add_trade_listener(self.event_queue)

    def add_gateway(self, gateway: ExchangeGateway):
        gateway.add_order_listener(self.event_queue)
        self.strategy.add_gateway(gateway)
        self.has_gateway = True

    def before_loop(self):
        for listener in self.status_listeners:
            self.strategy.add_status_listener(listener)

        warmup = Warmup(self.strategy, self.event_queue)
        warmup.run()

        self.register(EventType.MARKET_ORDER_BOOK, self.strategy.handle_order_book)
        self.register(EventType.MARKET_TRADE, self.strategy.handle_trade)
        self.register(EventType.MARKET_BAR, self.strategy.handle_bar)

        if self.has_gateway:
            self.register(EventType.ORDER_CREATED, self.strategy.handle_order_created)
            self.register(EventType.ORDER_CANCELLED, self.strategy.handle_order_cancelled)
            self.register(EventType.ORDER_FILLED, self.strategy.handle_order_filled)
            self.register(EventType.ORDER_ERROR, self.strategy.handle_order_error)

        self.register(EventType.TRADING_STOP, self.strategy.handle_stop)

        if self.reload_params:
            self.repeat(10, UpdateParamsTask())

        self.strategy.before_loop()

    def after_loop(self):
        self.strategy.after_loop()

    def stop(self):
        self.put(EventType.TRADING_STOP, None)


class Warmup(EventLoop):
    def __init__(self, strategy: Strategy, event_queue: EventQueue):
        super(Warmup, self).__init__(event_queue)
        self.strategy = strategy
        self.register(EventType.MARKET_ORDER_BOOK, self.warmup_order_book)
        self.register(EventType.MARKET_TRADE, self.warmup_trade)

    def warmup_order_book(self, order_book):
        self.strategy.warmup_order_book(order_book)
        if self.strategy.is_ready:
            raise EventLoopExit()

    def warmup_trade(self, trade):
        self.strategy.warmup_trade(trade)
        if self.strategy.is_ready:
            raise EventLoopExit()

    def after_loop(self):
        if not self.strategy.is_ready:
            raise Exception("Warmup ERROR: strategy not ready")
        LOG.info("Warmup DONE")


class UpdateParamsTask:
    PARAMS_FILE = "params.json"

    def __call__(self, caller: Trader):
        strategy = caller.strategy

        try:
            with open(self.PARAMS_FILE, "r", encoding="utf-8") as f:
                new_params = json.load(f)
        except FileNotFoundError:
            return
        except Exception:  # pragma: no cover - defensive
            LOG.exception("Failed to load %s", self.PARAMS_FILE)
            return

        if not isinstance(new_params, dict):
            LOG.warning("Invalid params format; expected dict got %s", type(new_params).__name__)
            return

        if strategy.update_params(new_params):
            LOG.info("Strategy params updated from %s", self.PARAMS_FILE)
