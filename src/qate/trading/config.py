"""What a run trades, as the strategy declares it.

A strategy's own `Config` dataclass extends `StrategyConfig` and answers two
questions for whatever is wiring the run up: which venues it needs an account on,
and which market data it wants. Those two answers are all a runner needs to build
connections and gateways without knowing anything about the strategy.

The config is *what a run trades* and is stable for the life of a deployment —
venues, markets, order sizes, limits. Parameters are *how it trades* and change
between runs; they arrive separately as a dict, so an optimizer can sweep them
without rewriting the config. `knowledge/03_writing_strategy.md` has the division.

This was `qate.boot.config.BootConfig`, in a module that also read an environment's
JSON files and knew where credentials live. That half is a deployment's business
and is now `qate-env`; the contract a strategy implements belongs here, beside the
`Strategy` it is handed to.
"""

from abc import ABC, abstractmethod

from qate.core.model import ExchangeName, Market


class StrategyConfig(ABC):
    """What a run trades: which venues, which markets, which event types."""

    @abstractmethod
    def get_exchanges(self) -> list[ExchangeName]:
        pass

    @abstractmethod
    def get_markets(self) -> list[tuple[Market, list[str]]]:
        pass
