"""Fees: the registry, discovery, the sign convention, and what an empty one costs.

`qate` holds no rate of its own, so what is worth testing is the mechanism and the two
things a reader could get wrong about it — an unregistered market is free and says so,
and rates arrive from an installed plugin without anyone importing it.

These tests own the global registry. The fixture empties it *and* marks discovery as
done, because on a developer's machine a fee plugin usually is installed and would
otherwise fill the registry in behind the test.
"""

import logging

import pytest

from qate.core.model import ExchangeName, Market, SettleType, Side
from qate.core.order import OrderRequest, OrderResponse, OrderState, OrderType
from qate.core.symbol import Symbol
from qate.trading import fee
from qate.trading.pnl_tracker import PnlTracker

MARKET = Market(ExchangeName.GMO, Symbol.BTC_SPOT)
OTHER = Market(ExchangeName.BITBANK, Symbol.XRP_SPOT)
TS = 1767225600.0

# There is no `OrderType.TAKER`: the type is DEFAULT, MAKER or STOP, and the taker
# rate is what everything that is not MAKER pays. That is the rule the table this
# replaced used too.
TAKER = OrderType.DEFAULT


@pytest.fixture(autouse=True)
def empty_registry(monkeypatch):
    monkeypatch.setattr(fee, "_SCHEDULES", {})
    monkeypatch.setattr(fee, "_WARNED", set())
    monkeypatch.setattr(fee, "_discovered", True)
    monkeypatch.delenv(fee.PLUGIN_ENV_VAR, raising=False)


def test_a_rate_registered_is_a_rate_charged():
    fee.register_rates(ExchangeName.GMO, Symbol.BTC_SPOT, maker=-0.0001, taker=0.0005)
    calc = fee.FeeCalculator()

    assert calc.calculate(MARKET, 1_000_000.0, TAKER) == pytest.approx(500.0)
    assert calc.calculate(MARKET, 1_000_000.0, OrderType.MAKER) == pytest.approx(-100.0)


def test_a_negative_maker_rate_is_a_rebate():
    """The sign is the whole convention: a maker fill can pay, and PnL subtracts it."""
    fee.register(MARKET, fee.FeeSchedule(maker=-0.0002, taker=0.0012))

    assert fee.FeeCalculator().calculate(MARKET, 500_000.0, OrderType.MAKER) < 0


def test_an_unregistered_market_is_free_and_warns_once(caplog):
    calc = fee.FeeCalculator()

    with caplog.at_level(logging.WARNING, logger="qate.trading.fee"):
        assert calc.calculate(MARKET, 1_000_000.0, TAKER) == 0.0
        assert calc.calculate(MARKET, 1_000_000.0, OrderType.MAKER) == 0.0

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1, "once per market, not once per fill"
    assert MARKET.id in warnings[0].message


def test_each_unregistered_market_warns_for_itself(caplog):
    calc = fee.FeeCalculator()

    with caplog.at_level(logging.WARNING, logger="qate.trading.fee"):
        calc.calculate(MARKET, 1.0, TAKER)
        calc.calculate(OTHER, 1.0, TAKER)

    assert len([r for r in caplog.records if r.levelno == logging.WARNING]) == 2


def test_zero_registered_is_not_the_same_as_unregistered(caplog):
    """A venue that charges nothing is an answer; silence is not."""
    fee.register_rates(ExchangeName.COINCHECK, Symbol.BTC_SPOT, maker=0.0, taker=0.0)
    coincheck = Market(ExchangeName.COINCHECK, Symbol.BTC_SPOT)

    with caplog.at_level(logging.WARNING, logger="qate.trading.fee"):
        assert fee.FeeCalculator().calculate(coincheck, 1_000_000.0, TAKER) == 0.0

    assert not [r for r in caplog.records if r.levelno == logging.WARNING]


def test_a_second_registration_replaces_the_first():
    """An account tier or a run's own assumption overrides what an adapter registered."""
    fee.register_rates(ExchangeName.GMO, Symbol.BTC_SPOT, maker=0.0, taker=0.0005)
    fee.register_rates(ExchangeName.GMO, Symbol.BTC_SPOT, maker=0.0, taker=0.0001)

    assert fee.get(MARKET).taker == 0.0001
    assert fee.registered_markets() == [MARKET.id]


def test_registering_late_still_reaches_a_tracker_already_built():
    """A `PnlTracker` built before any rate exists still charges the rate.

    It is constructed with its strategy, before anything has asked for a fee and so
    before discovery has run. A `FeeCalculator` that snapshotted the registry in
    `__init__` would charge nothing for the whole run.
    """
    tracker = PnlTracker()
    fee.register_rates(ExchangeName.GMO, Symbol.BTC_SPOT, maker=0.0, taker=0.001)

    tracker.process_order(fill(Side.BUY, SettleType.OPEN, 1.0, 100.0))
    update = tracker.process_order(fill(Side.SELL, SettleType.CLOSE, 1.0, 100.0))

    # Both legs, taker on a 100.0 notional each: the open leg's fee is deferred to
    # the close so one PnlUpdate carries the round trip.
    assert update.fee == pytest.approx(0.2)
    assert tracker.total_fees == pytest.approx(0.2)


def fill(side: Side, settle_type: SettleType, size: float, price: float) -> OrderResponse:
    request = OrderRequest(
        ts=TS,
        market=MARKET,
        side=side,
        price=price,
        size=size,
        settle_type=settle_type,
        order_type=TAKER,
    )
    return OrderResponse(
        request,
        state=OrderState.FILLED,
        created_ts=TS,
        exec_size=size,
        exec_price=price,
        completed_ts=TS + 1,
    )


# ------------------------------------------------------------------- discovery


def test_an_installed_plugin_is_found_without_anyone_importing_it(monkeypatch):
    """What makes rates reach a replay: nothing imports the package supplying them."""
    import fake_fee_plugin

    monkeypatch.setenv(fee.PLUGIN_ENV_VAR, "fake_fee_plugin")
    monkeypatch.setattr(fee, "_discovered", False)

    schedule = fee.get(Market(ExchangeName.HUOBI, Symbol.BTC_USDT))

    assert schedule is not None
    assert schedule.taker == fake_fee_plugin.TAKER


def test_discovery_happens_once(monkeypatch):
    calls = []
    monkeypatch.setattr(fee, "_discovered", False)
    monkeypatch.setattr(fee, "load_plugins", lambda *a, **k: calls.append(1))

    fee.get(MARKET)
    fee.get(MARKET)
    fee.registered_markets()

    assert len(calls) == 1


def test_a_broken_plugin_does_not_break_discovery(monkeypatch):
    monkeypatch.setenv(fee.PLUGIN_ENV_VAR, "no_such_module,fake_fee_plugin")
    monkeypatch.setattr(fee, "_discovered", False)

    assert fee.get(Market(ExchangeName.HUOBI, Symbol.BTC_USDT)) is not None


def test_an_explicit_registration_wins_over_a_plugins(monkeypatch):
    """A caller's own rate must not be overwritten by a plugin discovered later.

    `register` discovers first for this reason. Without it the override would be
    written, then the first lookup would load the plugin on top of it and the caller's
    rate would vanish with nothing said.
    """
    huobi = Market(ExchangeName.HUOBI, Symbol.BTC_USDT)
    monkeypatch.setenv(fee.PLUGIN_ENV_VAR, "fake_fee_plugin")
    monkeypatch.setattr(fee, "_discovered", False)

    fee.register(huobi, fee.FeeSchedule(maker=0.0, taker=0.004))

    assert fee.get(huobi).taker == 0.004
