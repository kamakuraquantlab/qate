from .model import ExchangeName
from .order import ExchangeExecution, ExchangeOrder, OrderRequest
from .symbol import Symbol


class Api:
    @property
    def exchange_name(self) -> ExchangeName:
        pass

    def fetch_balance(self, symbol: Symbol) -> dict | None:
        pass

    def fetch_executions(self, order_id: str, symbol: Symbol | None = None) -> list[ExchangeExecution]:
        pass

    def fetch_order(self, order_id: str, symbol: Symbol | None = None) -> ExchangeOrder:
        pass

    def create_order(self, order_request: OrderRequest) -> str:
        pass

    def change_order(self, order_request: OrderRequest) -> bool:
        pass

    def cancel_order(self, order_request: OrderRequest) -> bool:
        pass
