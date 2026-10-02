from abc import ABC, abstractmethod

from qate.core.model import ExchangeName, Market


class StrategyConfig(ABC):
    @abstractmethod
    def get_exchanges(self) -> list[ExchangeName]:
        pass

    @abstractmethod
    def get_markets(self) -> list[tuple[Market, list[str]]]:
        pass
