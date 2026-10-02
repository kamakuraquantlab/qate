import decimal
from enum import Enum


class Symbol(Enum):
    """A traded instrument. **The numbers are the wire format -- never reuse one.**

    Written out rather than `auto()`, and that is the whole point of this class being
    the way it is. A metric packs `symbol.value`, not its name, so with `auto()` the
    numbering came from position and deleting an unused member silently renumbered
    every member after it -- which silently relabels every metric file and every
    recorded msgpack already on disk. The gaps below are members that were deleted;
    leaving their numbers unused is what keeps the files written before then readable.

    Adding a symbol means taking the next free number. Deleting one is now safe, and
    means never giving its number to anything else.
    """

    BTC_JPY = 1
    ETH_JPY = 2
    XRP_JPY = 3
    # 4, 5 were BCH_JPY, LTC_JPY
    SOL_JPY = 6
    # 7, 8, 9 were ADA_JPY, DOGE_JPY, LINK_JPY

    BTC_SPOT = 10
    ETH_SPOT = 11
    XRP_SPOT = 12
    # 13, 14 were BCH_SPOT, LTC_SPOT
    SOL_SPOT = 15
    # 16-20 were DOGE_SPOT, ADA_SPOT, DOT_SPOT, MONA_SPOT, DAI_SPOT

    BTC_USDT = 21
    ETH_USDT = 22
    XRP_USDT = 23
    SOL_USDT = 24
    # 25-29 were BCH_USDT, LTC_USDT, ADA_USDT, DOGE_USDT, LINK_USDT


class Unit(Enum):
    U_10 = "10"
    U_1 = "1"
    U_01 = "0.1"
    U_001 = "0.01"
    U_0001 = "0.001"
    U_00001 = "0.0001"


class SymbolDef:
    def __init__(self, price_unit: Unit, size_unit: Unit, is_spot: bool = True):
        self.price_unit = float(price_unit.value)
        self.size_unit = float(size_unit.value)
        self.abs_tol = self.size_unit * 0.1
        self.is_spot = is_spot
        self._price_d = decimal.Decimal(price_unit.value)
        self._size_d = decimal.Decimal(size_unit.value)

    def fmt_price(self, side, price: float) -> str:
        rounding = decimal.ROUND_FLOOR
        if getattr(side, "name", None) != "BUY":
            rounding = decimal.ROUND_CEILING
        return str(decimal.Decimal(price).quantize(self._price_d, rounding=rounding))

    def fmt_size(self, size: float) -> str:
        return str(decimal.Decimal(size).quantize(self._size_d, rounding=decimal.ROUND_HALF_UP))


_SYMBOL_DEF = {
    # leverage
    Symbol.BTC_JPY: SymbolDef(Unit.U_1, Unit.U_0001, False),
    Symbol.ETH_JPY: SymbolDef(Unit.U_1, Unit.U_001, False),
    Symbol.XRP_JPY: SymbolDef(Unit.U_0001, Unit.U_10, False),
    Symbol.SOL_JPY: SymbolDef(Unit.U_01, Unit.U_01, False),
    # spot
    Symbol.BTC_SPOT: SymbolDef(Unit.U_1, Unit.U_00001),
    Symbol.ETH_SPOT: SymbolDef(Unit.U_1, Unit.U_0001),
    Symbol.XRP_SPOT: SymbolDef(Unit.U_0001, Unit.U_1),
    Symbol.SOL_SPOT: SymbolDef(Unit.U_01, Unit.U_001),
    # binance USDT
    Symbol.BTC_USDT: SymbolDef(Unit.U_001, Unit.U_0001),
    Symbol.ETH_USDT: SymbolDef(Unit.U_001, Unit.U_001),
    Symbol.XRP_USDT: SymbolDef(Unit.U_001, Unit.U_1),
    Symbol.SOL_USDT: SymbolDef(Unit.U_001, Unit.U_001),
}


def get_symbol_def(symbol: Symbol | str) -> SymbolDef:
    if isinstance(symbol, str):
        symbol = Symbol[symbol]
    return _SYMBOL_DEF[symbol]


__all__ = [
    "Symbol",
    "SymbolDef",
    "Unit",
    "get_symbol_def",
]
