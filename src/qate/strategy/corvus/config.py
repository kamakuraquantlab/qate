import json
from dataclasses import dataclass, field

from qate.core.ev_type import EventType
from qate.core.model import ExchangeName, Market, Side, Symbol
from qate.trading.config import StrategyConfig

# Maximum order size per (exchange, symbol).
# Requests larger than this are split into equal chunks.
MAX_ORDER_SIZE: dict[tuple[ExchangeName, Symbol], float] = {
    (ExchangeName.GMO,       Symbol.BTC_SPOT): 0.001,
    (ExchangeName.GMO,       Symbol.ETH_SPOT): 0.1,
    (ExchangeName.GMO,       Symbol.XRP_SPOT): 100.0,
    (ExchangeName.GMO,       Symbol.SOL_JPY):  1.0,
    (ExchangeName.BITBANK,   Symbol.BTC_SPOT): 0.001,
    (ExchangeName.BITBANK,   Symbol.ETH_SPOT): 0.1,
    (ExchangeName.BITBANK,   Symbol.XRP_SPOT): 100.0,
    (ExchangeName.COINCHECK, Symbol.BTC_SPOT): 0.001,
    (ExchangeName.COINCHECK, Symbol.ETH_SPOT): 0.1,
    (ExchangeName.COINCHECK, Symbol.XRP_SPOT): 100.0,
}


@dataclass
class OrderInstruction:
    exchange: ExchangeName
    symbol: Symbol
    side: Side
    amount: float


def _split(instr: OrderInstruction) -> list[OrderInstruction]:
    max_size = MAX_ORDER_SIZE.get((instr.exchange, instr.symbol))
    if max_size is None or instr.amount <= max_size + 1e-9:
        return [instr]
    chunks = []
    remaining = round(instr.amount, 8)
    while remaining > 1e-9:
        size = round(min(remaining, max_size), 8)
        chunks.append(OrderInstruction(instr.exchange, instr.symbol, instr.side, size))
        remaining = round(remaining - size, 8)
    return chunks


@dataclass
class Config(StrategyConfig):
    instructions: list[OrderInstruction] = field(default_factory=list)
    reprice_interval: float = 5.0

    def get_exchanges(self) -> list[ExchangeName]:
        return list(dict.fromkeys(i.exchange for i in self.instructions))

    def get_markets(self) -> list[tuple[Market, list[str]]]:
        seen = set()
        markets = []
        for i in self.instructions:
            mid = Market(i.exchange, i.symbol).id
            if mid not in seen:
                seen.add(mid)
                markets.append((Market(i.exchange, i.symbol), [EventType.MARKET_ORDER_BOOK]))
        return markets

    @classmethod
    def from_jsonl(cls, path: str, reprice_interval: float = 5.0) -> "Config":
        raw = []
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                d = json.loads(line)
                if "type" in d:
                    continue  # skip result lines written by a previous run
                raw.append(OrderInstruction(
                    exchange=ExchangeName[d["exchange"].upper()],
                    symbol=Symbol[d["symbol"].upper()],
                    side=Side[d["side"].upper()],
                    amount=float(d["amount"]),
                ))
        instructions = []
        for instr in raw:
            instructions.extend(_split(instr))
        return cls(instructions=instructions, reprice_interval=reprice_interval)
