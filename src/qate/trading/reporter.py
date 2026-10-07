from qate.core.feed import StatusFeed
from qate.core.order import OrderResponse


class Reporter(StatusFeed):
    """A destination for a run's outcomes. Override the hooks you want."""

    def start(self) -> None:
        """Open the destination and report that the run started."""

    def stop(self) -> None:
        """Report that the run stopped and close the destination."""

    def report_order(self, order_response: OrderResponse) -> None:
        """An order was filled."""

    def report_metrics(self, metrics: list) -> None:
        """A batch of strategy metrics was emitted."""

    def report_pnl(self, pnl_update) -> None:
        """A position was opened or closed. `settle_type` says which."""

    def report_exception(self, error: BaseException) -> None:
        """Something failed. Worth routing somewhere noisier than the rest."""

    def report_message(self, message: str) -> None:
        """Free text a strategy asked to be passed on."""
