"""Telling someone outside the process what a run is doing.

A `MetricLog` records everything for later. A `Reporter` is the other direction:
the handful of things a person wants to see while a strategy is running -- it
filled, it closed a position, it broke -- delivered somewhere they are looking.
Discord, a webhook, a log line, standard output.

Whatever runs a strategy fans its outcomes out to every reporter added to it, so a
strategy never knows one exists.

## One callback per kind of outcome

```python
class MyReporter(Reporter):
    def on_order(self, order_response):
        print(order_response.summary)
```

Every hook is a no-op by default: implement the ones you care about. That is the
point of the shape. Its predecessor was a `Chat` with a single `send(data)` that
sniffed its argument -- `isinstance(data, OrderResponse)`, then `BaseException`,
then `str`, then a fallback that reported "unexpected data type" to an error
channel. Adding a kind of outcome meant extending a type switch inside every
implementation, and passing the wrong thing was discovered at runtime by the
recipient.

## Naming

Not "channel": Enoshima already has a `Channel` that is a queue of market events,
and the word means a queue in most of the languages nearby. Not "sink" either --
that is `MetricLog`'s job, which is storage. This reports.

## It may also listen

`Reporter` extends `StatusFeed`, so an implementation that is bidirectional -- a
chat bot taking commands, say -- can publish `EventType.MSG_IN` back to whatever
is running it. A reporter that only reports never touches it.
"""

from qate.core.feed import StatusFeed
from qate.core.order import OrderResponse


class Reporter(StatusFeed):
    """A destination for a run's outcomes. Override the hooks you want."""

    def connect(self) -> None:
        """Open whatever connection this needs, before the run starts."""

    def disconnect(self) -> None:
        """Close it. Called once, during shutdown, after the last report."""

    def on_start(self) -> None:
        """The run has started.

        No argument: a reporter is constructed knowing which run it speaks for, and
        `Runtime` is a `Thread`, so its `name` is the thread's.
        """

    def on_stop(self) -> None:
        """The run is stopping. The last thing called before `disconnect`."""

    def on_order(self, order_response: OrderResponse) -> None:
        """An order was filled."""

    def on_pnl_update(self, pnl_update) -> None:
        """A position was opened or closed. `settle_type` says which."""

    def on_summary(self, market_id: str, summary: str) -> None:
        """Running totals for one market, after a position closed.

        Separate from `on_pnl_update` because it is an aggregate the runner keeps,
        not a fact about one trade, and a reporter may well want one without the
        other -- a per-trade feed is noisy, a periodic summary is not.
        """

    def on_exception(self, error: BaseException) -> None:
        """Something failed. Worth routing somewhere noisier than the rest."""

    def on_message(self, message: str) -> None:
        """Free text a strategy asked to be passed on."""
