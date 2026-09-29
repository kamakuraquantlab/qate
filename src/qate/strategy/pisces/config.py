from dataclasses import dataclass, field

from qate.boot.config import BootConfig
from qate.core.ev_type import EventType
from qate.core.model import ExchangeName, Market, Symbol


@dataclass
class PairConfig:
    symbol: Symbol
    order_size: float
    allocated_value_jpy: float = 100_000.0


@dataclass
class Config(BootConfig):
    maker_exchange: ExchangeName
    taker_exchange: ExchangeName
    pairs: list[PairConfig] = field(default_factory=list)
    min_jpy_to_keep: dict[str, float] = field(default_factory=dict)
    max_orders: int = 1

    def get_exchanges(self) -> list[ExchangeName]:
        return [self.maker_exchange, self.taker_exchange]

    def get_markets(self) -> list[tuple[Market, list[str]]]:
        markets = []
        for pc in self.pairs:
            markets.append((Market(self.maker_exchange, pc.symbol), [EventType.MARKET_ORDER_BOOK]))
            markets.append((Market(self.taker_exchange, pc.symbol), [EventType.MARKET_ORDER_BOOK]))
        return markets


def default_config() -> Config:
    return Config(
        maker_exchange=ExchangeName.BITBANK,
        taker_exchange=ExchangeName.GMO,
        pairs=[
            PairConfig(symbol=Symbol.XRP_SPOT, order_size=100, allocated_value_jpy=100_000.0),
            PairConfig(symbol=Symbol.BTC_SPOT, order_size=0.001, allocated_value_jpy=100_000.0),
        ],
    )
