"""A stand-in fee provider, for the discovery tests.

Registers one market and imports nothing that could reach a venue, which is the
shape the `qate.fees` entry point asks for.
"""

from qate.core.model import ExchangeName
from qate.core.symbol import Symbol
from qate.trading import fee

MAKER = -0.0003
TAKER = 0.0009


def register_fees() -> None:
    fee.register_rates(ExchangeName.HUOBI, Symbol.BTC_USDT, maker=MAKER, taker=TAKER)
