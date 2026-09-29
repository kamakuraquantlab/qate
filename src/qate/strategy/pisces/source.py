from logging import getLogger

from qate.core.model import Market, MarketPrice, OrderBook, SettleType, Side
from qate.core.order import OrderRequest, OrderResponse, OrderType
from qate.core.symbol import get_symbol_def
from qate.trading.gateways import DefaultGateway
from qate.trading.inventory import SpotInventory

LOG = getLogger(__name__)


class Source:
    def __init__(self, market: Market, inventory: SpotInventory):
        self.market = market
        self.inventory = inventory
        self.gateway: DefaultGateway = None
        self.market_price: MarketPrice = None
        self.order_book: OrderBook = None
        self.symbol_def = get_symbol_def(self.market.symbol)

    def _create(
        self,
        ts: float,
        side: Side,
        order_size: float,
        price: float,
        settle_type: SettleType,
        order_type: OrderType,
    ) -> OrderRequest:
        order_request = OrderRequest(ts, self.market, side, price, order_size, settle_type, order_type)
        self.gateway.create(order_request)
        self.inventory.reserve(order_request)
        return order_request


class MakerSource(Source):
    def __init__(self, market: Market, order_size: float, inventory: SpotInventory):
        super().__init__(market, inventory)
        self.order_size = order_size

    def attempt_buy(self, ts: float, price: float, settle_type: SettleType) -> OrderRequest | None:
        if self.inventory.can_buy(self.order_size, self.market_price.bid):
            LOG.info(
                f"can_buy {self.market.id} available_quote={self.inventory.available_quote:.3f} "
                f"size={self.order_size:.3f} price={price:.3f}"
            )
            return self._create(ts, Side.BUY, self.order_size, price, settle_type, OrderType.MAKER)
        return None

    def attempt_sell(self, ts: float, price: float, settle_type: SettleType) -> OrderRequest | None:
        if self.inventory.can_sell(self.order_size):
            LOG.info(
                f"can_sell {self.market.id} available_base={self.inventory.available_base:.6f} "
                f"size={self.order_size:.6f}"
            )
            return self._create(ts, Side.SELL, self.order_size, price, settle_type, OrderType.MAKER)
        return None

    def cancel(self, order_response: OrderResponse):
        order_request = order_response.order_request
        order_request.order_id = order_response.order_id
        self.gateway.cancel(order_request)


class TakerSpotSource(Source):
    def __init__(self, market: Market, inventory: SpotInventory):
        super().__init__(market, inventory)

    def buy(self, ts: float, order_size: float, settle_type: SettleType) -> OrderRequest:
        return self._create(ts, Side.BUY, order_size, self.market_price.ask, settle_type, OrderType.DEFAULT)

    def sell(self, ts: float, order_size: float, settle_type: SettleType) -> OrderRequest:
        return self._create(ts, Side.SELL, order_size, self.market_price.bid, settle_type, OrderType.DEFAULT)
