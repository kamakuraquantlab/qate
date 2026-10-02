import decimal
from enum import Enum


class Symbol(Enum):
    BTC_JPY = 1
    ETH_JPY = 2
    XRP_JPY = 3
    SOL_JPY = 6

    BTC_SPOT = 10
    ETH_SPOT = 11
    XRP_SPOT = 12
    SOL_SPOT = 15

    BTC_USDT = 21
    ETH_USDT = 22
    XRP_USDT = 23
    SOL_USDT = 24


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
