from qate.core.model import Metric
from qate.core.order import OrderResponse


class Reporter:
    """A destination for a run's outcomes. Override the hooks you want.

    ``Runtime`` passes each outcome directly. ``Runtime2`` passes a
    :class:`qate.trading.runtime2.StrategyEvent` so a shared reporter can retain
    the strategy instance that produced it.
    """

    def start(self) -> None:
        """Open the destination and report that the run started."""

    def stop(self) -> None:
        """Report that the run stopped and close the destination."""

    def report_order(self, order_response: OrderResponse) -> None:
        """An order was filled."""

    def report_metrics(self, metrics: list[Metric]) -> None:
        """A batch of strategy metrics was emitted."""

    def report_pnl(self, pnl_update) -> None:
        """A position was opened or closed. `settle_type` says which."""

    def report_exception(self, error: BaseException) -> None:
        """Something failed. Worth routing somewhere noisier than the rest."""
