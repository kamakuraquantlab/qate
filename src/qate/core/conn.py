from abc import ABC, abstractmethod

from .feed import ExchangeFeed, MarketDataFeed, StatusFeed
from .model import ExchangeName


class Connection(StatusFeed, ABC):
    """A long-lived connection to a venue, started and stopped by the runtime."""

    @property
    @abstractmethod
    def exchange_name(self) -> ExchangeName:
        pass

    @abstractmethod
    def connect(self) -> None:
        pass

    @abstractmethod
    def disconnect(self) -> None:
        pass


class PublicConnection(Connection, MarketDataFeed):
    """Public market data: order books and trades, no credential."""


class PrivateConnection(Connection, ExchangeFeed):
    """Account data: the venue's own executions and order updates."""
