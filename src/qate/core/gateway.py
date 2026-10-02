from abc import ABC, abstractmethod

from .feed import ExchangeFeed, MarketDataFeed, OrderFeed
from .model import ExchangeName
from .order import OrderRequest


class ExchangeGateway(OrderFeed, ABC):
    @property
    @abstractmethod
    def exchange_name(self) -> ExchangeName:
        pass

    @abstractmethod
    def create(self, order_request: OrderRequest) -> None:
        """Accept an order. The outcome arrives through `OrderFeed`, not as a return."""

    @abstractmethod
    def cancel(self, order_request: OrderRequest) -> None:
        """Accept a cancellation. The outcome arrives through `OrderFeed`."""

    def subscribe_exchange_feed(self, exchange_feed: ExchangeFeed) -> None:
        """Take the venue's own execution and order stream, where there is one.

        Optional: a venue without an account feed is reconciled by polling
        instead, and a simulator has no feed to take.
        """

    def subscribe_market_data_feed(self, market_data_feed: MarketDataFeed) -> None:
        """Take market data, for a gateway that needs it to decide fills.

        Optional, and in practice only the simulator does.
        """
