"""Connection interfaces an exchange adapter implements.

These are declarations, not implementations. `qate` describes the shape of a
market-data feed and an account feed so that the rest of the library -- the
gateway, the boot configurator, a strategy -- can be written against a venue it
never names. The transport itself (a WebSocket client, reconnect policy, an
order-book diff tracker) belongs to whichever adapter package supplies the
venue, and lives there.

The split is deliberate and is what makes this package safe to publish: a
`qate` install carries no venue endpoint and no WebSocket client, so there is
nothing here that can be pointed at a live exchange. `qate.simulator` is the
only feed this package can produce on its own.
"""

from abc import ABC, abstractmethod

from .feed import ExchangeFeed, MarketDataFeed, StatusFeed
from .model import ExchangeName


class Connection(StatusFeed, ABC):
    """A long-lived connection to a venue, started and stopped by the runtime.

    An implementation is expected to reconnect on its own and to publish
    `EventType.CONN_*` through `StatusFeed`; nothing above this interface
    retries on its behalf.
    """

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
