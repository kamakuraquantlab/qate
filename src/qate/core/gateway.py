"""What a strategy needs from a venue in order to trade: the gateway contract.

A gateway takes order requests and publishes what became of them. That is all a
strategy knows about it, and all this declares:

- `create` and `cancel` take an `OrderRequest`.
- The outcome arrives later through `OrderFeed`, as `ORDER_CREATED`,
  `ORDER_FILLED`, `ORDER_CANCELLED` or `ORDER_ERROR`. Never as a return value --
  an exchange answers asynchronously and a strategy has to be written for that.
- `exchange_name` says which venue it speaks for.

## Deliberately no event loop

This used to extend `EventLoop`, so `create()` meant "put an event on my queue for
the thread I am running on" rather than "place this order". That is one way of
satisfying the contract, not the contract itself, and baking it in had two costs.

An adapter author inherited a thread whether or not the venue needed one. And a
backtest, which wants the fill computed inside the strategy's own call, had to
supply a fake queue that dispatched synchronously just to defeat the machinery --
a whole class existing to undo a decision made here.

`qate.trading.gateways.QueuedGateway` is the event-loop implementation, and every
live venue adapter builds on it. `qate.simulator.SimulatorGateway` implements this
contract directly and fills in place.

The split also lets a gateway be a *composite*: an adapter that fans open and close
orders out to two loops of its own is a gateway, and could not say so while the
contract was itself a loop -- inheriting it would have added a third thread that
nothing read.

## A live runtime needs more than this

`start`, `stop` and `join` are not here. This is what a *strategy* needs; running a
gateway as a long-lived process needs a lifecycle too, and `qate.boot.Bootstrap`
calls those three. `QueuedGateway` has them from `EventLoop`, and a composite
forwards them. A simulator has nothing to start, which is exactly why they do not
belong in the contract.
"""

from abc import ABC, abstractmethod

from .feed import ExchangeFeed, MarketDataFeed, OrderFeed
from .model import ExchangeName
from .order import OrderRequest

# The event types `QueuedGateway` dispatches on. Defined here because they are
# part of the vocabulary an adapter overriding the queued path needs to name.
EVENT_CREATE_ORDER = "EVENT_CREATE_ORDER"
EVENT_CANCEL_ORDER = "EVENT_CANCEL_ORDER"


class ExchangeGateway(OrderFeed, ABC):
    """The interface a strategy places orders through."""

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
