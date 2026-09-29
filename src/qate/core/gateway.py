from .api import Api
from .ev_loop import EventLoop
from .feed import ExchangeFeed, MarketDataFeed, OrderFeed
from .model import ExchangeName
from .order import OrderRequest

EVENT_CREATE_ORDER = "EVENT_CREATE_ORDER"
EVENT_CANCEL_ORDER = "EVENT_CANCEL_ORDER"


class ExchangeGateway(EventLoop, OrderFeed):
    def __init__(self, api: Api, heartbeat_interval_ts: float = None):
        EventLoop.__init__(self, None, heartbeat_interval_ts)
        OrderFeed.__init__(self)
        self.api = api

        self.register(EVENT_CREATE_ORDER, self.handle_create_order)
        self.register(EVENT_CANCEL_ORDER, self.handle_cancel_order)

    @property
    def exchange_name(self) -> ExchangeName:
        return self.api.exchange_name

    def create(self, order_request: OrderRequest):
        self.put(EVENT_CREATE_ORDER, order_request)

    def cancel(self, order_request: OrderRequest):
        self.put(EVENT_CANCEL_ORDER, order_request)

    def handle_create_order(self, order_request: OrderRequest):
        pass

    def handle_cancel_order(self, order_request: OrderRequest):
        pass

    def subscribe_exchange_feed(self, exchange_feed: ExchangeFeed):
        pass

    def subscribe_market_data_feed(self, market_data_feed: MarketDataFeed):
        pass
