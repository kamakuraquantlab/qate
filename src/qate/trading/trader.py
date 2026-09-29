import json
from logging import getLogger

from qate.core.ev_loop import EventLoop
from qate.core.ev_q import EventQueue
from qate.core.ev_type import EventType
from qate.core.feed import MarketDataFeed
from qate.core.model import EventLoopExit

from .gateway import ExchangeGateway
from .strategy import Strategy

LOG = getLogger(__name__)


class Trader(EventLoop):
    def __init__(self, strategy: Strategy, event_queue: EventQueue = None):
        super(Trader, self).__init__(event_queue)
        self.strategy = strategy
        self.has_gateway = False

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

        self.register(EventType.MSG_IN, self.strategy.handle_msg)
        self.register(EventType.TRADING_STOP, self.strategy.handle_stop)

        self.repeat(10, UpdateParamsTask())

        self.strategy.before_loop()

    def after_loop(self):
        self.strategy.after_loop()

    def notify(self, msg: str):
        self.put(EventType.MSG_IN, msg)

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
        except Exception as exc:  # pragma: no cover - defensive
            LOG.error("Failed to load %s: %s", self.PARAM_FILE, exc)
            return

        if not isinstance(new_params, dict):
            LOG.warning("Invalid params format; expected dict got %s", type(new_params).__name__)
            return

        if strategy.update_params(new_params):
            LOG.info("Strategy params updated from %s", self.PARAMS_FILE)
