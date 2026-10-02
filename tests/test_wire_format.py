"""The numbers a metric file is written with, pinned.

A `Metric` packs enum *values*, not names: `[measurement, ts, tags, fields]` with
integer keys. So these five enums are the wire format of every metric file and every
recorded msgpack on disk, and a value that changes meaning does not fail -- it
relabels history, silently, and a backtest over old data quietly answers about the
wrong market.

`AGENTS.md` has said "append-only" about them for a long time and nothing enforced it.
This does. It is a transcription of what the numbers are today, so a member that is
renumbered, reordered or deleted fails here with the old number next to the new one.

When you genuinely mean to change the wire format, update this table in the same
commit and say in the message what becomes unreadable. When you are adding a symbol or
a field, append it here with the next free number and nothing else moves.

`Symbol` is the one that already has explicit values, after an `auto()` renumbering
was caught on its way in; the others still number by position, so for them this file
is the only thing standing between a tidy-up and a corrupted archive.
"""

import pytest

from qate.core.model import ExchangeName, Field, Measurement, Tag
from qate.core.symbol import Symbol

ExchangeName_VALUES = {
    "GMO": 1,
    "COINCHECK": 2,
    "BITBANK": 3,
    "HUOBI": 4,
    "BINANCE": 5,
    "RAKUTEN": 6,
    "SIMULATOR": 7,
}
Symbol_VALUES = {
    "BTC_JPY": 1,
    "ETH_JPY": 2,
    "XRP_JPY": 3,
    "SOL_JPY": 6,
    "BTC_SPOT": 10,
    "ETH_SPOT": 11,
    "XRP_SPOT": 12,
    "SOL_SPOT": 15,
    "BTC_USDT": 21,
    "ETH_USDT": 22,
    "XRP_USDT": 23,
    "SOL_USDT": 24,
}
Measurement_VALUES = {
    "TRADING": 1,
    "TRADE": 2,
    "MARKET_PRICE": 3,
    "ORDER": 4,
    "PNL": 5,
    "MARKET_DATA_DELAY": 6,
    "API_LATENCY": 7,
    "SYS": 8,
    "EVAL": 9,
    "BAR": 10,
}
Tag_VALUES = {
    "EXCHANGE_NAME": 1,
    "SYMBOL": 2,
    "SIDE": 3,
    "SETTLE_TYPE": 4,
    "ORDER_TYPE": 5,
    "DATA_TYPE": 6,
    "DESC": 7,
}
Field_VALUES = {
    "PRICE": 1,
    "SIZE": 2,
    "BID": 3,
    "ASK": 4,
    "SPREAD": 5,
    "EXEC_PRICE": 6,
    "EXEC_SIZE": 7,
    "SLIPPAGE": 8,
    "TIME_TO_CREATE": 9,
    "TIME_TO_FILL": 10,
    "PNL": 11,
    "TOTAL": 12,
    "RETURN": 13,
    "FEE": 14,
    "VALUE": 15,
}


@pytest.mark.parametrize(
    "enum,pinned",
    [
        (ExchangeName, ExchangeName_VALUES),
        (Symbol, Symbol_VALUES),
        (Measurement, Measurement_VALUES),
        (Tag, Tag_VALUES),
        (Field, Field_VALUES),
    ],
    ids=lambda x: getattr(x, "__name__", ""),
)
def test_the_wire_values_have_not_moved(enum, pinned):
    actual = {member.name: member.value for member in enum}

    moved = {
        name: (pinned[name], actual[name])
        for name in pinned.keys() & actual.keys()
        if pinned[name] != actual[name]
    }
    assert not moved, (
        f"{enum.__name__} members changed value (was, now): {moved}. "
        f"Every metric file already written means the old number."
    )

    gone = pinned.keys() - actual.keys()
    assert not gone, (
        f"{enum.__name__} members removed: {sorted(gone)}. That is allowed, but their "
        f"numbers must never be reused -- leave a gap, update this table, and say so."
    )


def test_a_new_member_takes_a_free_number():
    """Added members are fine; they must not take a number this table already knows."""
    for enum, pinned in (
        (ExchangeName, ExchangeName_VALUES),
        (Symbol, Symbol_VALUES),
        (Measurement, Measurement_VALUES),
        (Tag, Tag_VALUES),
        (Field, Field_VALUES),
    ):
        taken = {v: k for k, v in pinned.items()}
        for member in enum:
            if member.name in pinned:
                continue
            assert member.value not in taken, (
                f"{enum.__name__}.{member.name} reuses {member.value}, which "
                f"{taken[member.value]} had. Old files would read as the wrong thing."
            )
