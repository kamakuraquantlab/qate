"""Fees: the registry, the sign convention, and what an empty registry costs.

`qate` holds no rate of its own, so the thing worth testing is the mechanism and the
one behaviour a reader could get wrong about it — an unregistered market is free, and
says so. These tests own the global registry, so each one clears it.
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
def empty_registry():
    fee.clear()
    yield
    fee.clear()


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
    """The ordering a live runner actually has: strategy first, adapters after.

    `PnlTracker` is constructed with the strategy, which happens before the gateways
    that cause an adapter package to be imported. A `FeeCalculator` that snapshotted
    the registry in `__init__` would charge nothing for the whole run.
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
