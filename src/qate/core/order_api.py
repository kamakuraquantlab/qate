from abc import ABC, abstractmethod

from .order import ExchangeExecution, OrderRequest
from .symbol import Symbol


class OrderApi(ABC):
    """Async order operations used by non-blocking gateways."""

    @abstractmethod
    async def create_order(self, order_request: OrderRequest) -> str | None:
        """Create a new order and return the exchange order id."""

    @abstractmethod
    async def cancel_order(self, order_request: OrderRequest) -> bool:
        """Cancel an existing order and return True when the exchange accepted the request."""

    @abstractmethod
    async def fetch_executions(self, order_id: str, symbol: Symbol | None = None) -> list[ExchangeExecution]:
        """Return executions for the order, used during reconciliation."""

    async def aclose(self) -> None:
        """Hook for closing underlying resources. Optional for implementations."""
        return None
