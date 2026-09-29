"""The two shipped strategies load, construct, and satisfy the contract they document.

Example code that no longer runs is worse than no example, and a strategy is loaded
by name at runtime — so a rename inside `qate.trading` would not break anything
until someone tried to use one. These tests are cheap and catch exactly that.

They stop short of replaying a strategy: what `pisces` does with a fill is
Enoshima's to exercise, and `corvus` needs a live gateway to be interesting.
"""

import json

import pytest

from qate.boot.config import BootConfig
from qate.boot.strategy_loader import get_strategy_class
from qate.core.ev_type import EventType
from qate.core.model import ExchangeName, Side
from qate.core.symbol import Symbol
from qate.trading.strategy import Strategy

SHIPPED = [
    ("qate.strategy.corvus", "v1"),
    ("qate.strategy.pisces", "v1"),
    ("qate.strategy.pisces", "v2"),
]


@pytest.mark.parametrize("module_name,variant", SHIPPED)
def test_the_loader_finds_a_variant(module_name, variant):
    """Exactly one class named Variant per file, extending Strategy."""
    cls = get_strategy_class(module_name, variant)
    assert cls.__name__ == "Variant"
    assert issubclass(cls, Strategy)


# ------------------------------------------------------------------------ corvus


def corvus_config(tmp_path, lines):
    from qate.strategy.corvus.config import Config

    path = tmp_path / "orders.jsonl"
    path.write_text("".join(json.dumps(line) + "\n" for line in lines))
    return Config.from_jsonl(str(path))


def test_corvus_reads_a_jsonl_of_orders(tmp_path):
    config = corvus_config(
        tmp_path,
        [
            {"exchange": "BITBANK", "symbol": "XRP_SPOT", "side": "BUY", "amount": 50.0},
            {"exchange": "GMO", "symbol": "BTC_SPOT", "side": "SELL", "amount": 0.0005},
        ],
    )
    assert len(config.instructions) == 2
    first = config.instructions[0]
    assert (first.exchange, first.symbol, first.side, first.amount) == (
        ExchangeName.BITBANK,
        Symbol.XRP_SPOT,
        Side.BUY,
        50.0,
    )


def test_corvus_splits_an_order_above_the_venue_maximum(tmp_path):
    """MAX_ORDER_SIZE for GMO BTC_SPOT is 0.001, so 0.0025 becomes three chunks."""
    config = corvus_config(
        tmp_path,
        [{"exchange": "GMO", "symbol": "BTC_SPOT", "side": "BUY", "amount": 0.0025}],
    )
    amounts = [i.amount for i in config.instructions]
    assert amounts == [0.001, 0.001, 0.0005]
    assert sum(amounts) == pytest.approx(0.0025)


def test_corvus_skips_result_lines_from_a_previous_run(tmp_path):
    """A file may already carry output lines; instructions are the ones without a type."""
    config = corvus_config(
        tmp_path,
        [
            {"exchange": "GMO", "symbol": "XRP_SPOT", "side": "BUY", "amount": 10.0},
            {"type": "result", "filled": True},
        ],
    )
    assert len(config.instructions) == 1


def test_corvus_config_declares_its_markets_once_each(tmp_path):
    config = corvus_config(
        tmp_path,
        [
            {"exchange": "GMO", "symbol": "XRP_SPOT", "side": "BUY", "amount": 10.0},
            {"exchange": "GMO", "symbol": "XRP_SPOT", "side": "BUY", "amount": 10.0},
            {"exchange": "BITBANK", "symbol": "XRP_SPOT", "side": "SELL", "amount": 10.0},
        ],
    )
    markets = config.get_markets()
    assert [m.id for m, _ in markets] == ["GMO:XRP_SPOT", "BITBANK:XRP_SPOT"]
    assert all(events == [EventType.MARKET_ORDER_BOOK] for _, events in markets)
    assert config.get_exchanges() == [ExchangeName.GMO, ExchangeName.BITBANK]


def test_corvus_constructs_and_starts_unready(tmp_path):
    """It is ready only once a book has arrived for every market it must trade."""
    config = corvus_config(
        tmp_path, [{"exchange": "GMO", "symbol": "XRP_SPOT", "side": "BUY", "amount": 10.0}]
    )
    strategy = get_strategy_class("qate.strategy.corvus", "v1")(config, {})
    assert isinstance(strategy, Strategy)
    assert strategy.is_ready is False
    assert len(strategy.pending) == 1


# ------------------------------------------------------------------------ pisces


def pisces_config():
    from qate.strategy.pisces.config import default_config

    return default_config()


def test_pisces_default_config_is_a_bootconfig():
    config = pisces_config()
    assert isinstance(config, BootConfig)
    assert config.get_exchanges() == [ExchangeName.BITBANK, ExchangeName.GMO]


def test_pisces_replays_both_venues_of_every_pair():
    """A cross-venue strategy is only meaningful with both books subscribed."""
    config = pisces_config()
    markets = config.get_markets()
    assert len(markets) == 2 * len(config.pairs)
    for pair in config.pairs:
        ids = {m.id for m, _ in markets}
        assert f"BITBANK:{pair.symbol.name}" in ids
        assert f"GMO:{pair.symbol.name}" in ids
    assert all(events == [EventType.MARKET_ORDER_BOOK] for _, events in markets)


@pytest.mark.parametrize("variant", ["v1", "v2"])
def test_pisces_constructs_from_its_documented_params(variant):
    """The params table in knowledge/strategies.md is what a run actually passes."""
    params = {
        "default_profit_margin_percentage": 0.0007,
        "transfer_profit_margin_percentage": 0.0001,
        "price_tolerance_percentage": 0.0001,
    }
    strategy = get_strategy_class("qate.strategy.pisces", variant)(pisces_config(), params)
    assert isinstance(strategy, Strategy)
    assert strategy.get_param_set_id()


def test_pisces_schedule_closes_the_maintenance_window():
    """Saturday morning is a venue maintenance window, not a trading opportunity."""
    from datetime import datetime

    from qate.core.model import TradingMode
    from qate.strategy.pisces.schedule import Schedule

    schedule = Schedule()
    # 2026-01-03 is a Saturday.
    assert schedule.check(datetime(2026, 1, 3, 8, 30).timestamp()) == TradingMode.NO_ENTRY
    assert schedule.check(datetime(2026, 1, 3, 10, 0).timestamp()) == TradingMode.NO_TRADING
    assert schedule.check(datetime(2026, 1, 3, 13, 0).timestamp()) == TradingMode.DEFAULT
    # 2026-01-05 is a Monday.
    assert schedule.check(datetime(2026, 1, 5, 10, 0).timestamp()) == TradingMode.DEFAULT
