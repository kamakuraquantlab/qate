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
