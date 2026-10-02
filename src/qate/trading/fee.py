"""What a fill costs: the mechanism here, the numbers from whoever knows them.

A fee rate is a venue's fact. It is published on a venue's own fee page, changes
when the venue decides, and differs per account tier -- so a table of rates in a
library is a table that is wrong somewhere and silently. `qate` therefore holds no
rate at all. It holds the shape of one, a registry to put them in, and the
arithmetic that turns a rate into a cost.

`qate-exchanges` registers the venues it adapts, at import, which is the same
argument as `qate.exchange.registry`: the package that speaks to a venue is the
package that tracks what that venue charges.

## Registering

```python
from qate.core.model import ExchangeName, Symbol
from qate.trading import fee

fee.register_rates(ExchangeName.GMO, Symbol.BTC_SPOT, maker=-0.0001, taker=0.0005)
```

Rates are fractions of notional, and **a negative maker rate is a rebate** -- the
venue pays for the liquidity. That sign convention is why the field is not called
`cost`: `FeeCalculator.calculate` returns a negative number for a maker fill on a
rebating venue, and a `PnlUpdate` that subtracts it is correct.

## An unregistered market costs nothing, loudly

`calculate` returns `0.0` for a market no one registered, and warns once per market.
It does not raise: a venue whose fees a run does not know is a configuration gap,
not a logic bug, and a live strategy must not die mid-position over one.

The consequence is worth being plain about, because it decides what a result means.
A process that registers nothing -- a backtest with no adapter installed, which is
the usual case -- computes **zero fees**, and a strategy whose edge is thinner than
its fees will look profitable. Fees were hardcoded here once, so this could not
happen; the cost of their being right is that something now has to supply them.
`registered_markets()` is there to be asserted on before a run that cares.
"""

from dataclasses import dataclass
from logging import getLogger

from qate.core.model import ExchangeName, Market
from qate.core.order import OrderType
from qate.core.symbol import Symbol

LOG = getLogger(__name__)


@dataclass(frozen=True)
class FeeSchedule:
    """One market's maker and taker rates, as fractions of notional.

    Negative is a rebate. Zero is a real answer and has to be registered like any
    other: "this venue charges nothing" and "nobody told us" must not look alike.
    """

    maker: float
    taker: float

    def rate(self, order_type: OrderType) -> float:
        return self.maker if order_type == OrderType.MAKER else self.taker


_SCHEDULES: dict[str, FeeSchedule] = {}
_WARNED: set[str] = set()


def register(market: Market, schedule: FeeSchedule) -> None:
    """Record what one market charges. A second registration replaces the first.

    Replacing is deliberate and not warned about, unlike an adapter: an account tier,
    a campaign or a run's own assumption is a legitimate reason to override what an
    adapter registered, and the last word belongs to whoever is closest to the run.
    """
    _SCHEDULES[market.id] = schedule
    _WARNED.discard(market.id)


def register_rates(exchange_name: ExchangeName, symbol: Symbol, maker: float, taker: float) -> None:
    """`register` for a caller holding rates rather than a `FeeSchedule`."""
    register(Market(exchange_name, symbol), FeeSchedule(maker=maker, taker=taker))


def get(market: Market) -> FeeSchedule | None:
    """One market's schedule, or `None` if nothing registered it."""
    return _SCHEDULES.get(market.id)


def registered_markets() -> list[str]:
    """The market ids with a schedule. Assert on this before a run that needs fees."""
    return sorted(_SCHEDULES)


def clear() -> None:
    """Forget every schedule. For tests, and for a process rebuilding its own."""
    _SCHEDULES.clear()
    _WARNED.clear()


class FeeCalculator:
    """Notional and order type in, cost out.

    It reads the registry on every call rather than snapshotting it at construction.
    That matters for ordering: a live runner builds its strategy -- and with it the
    `PnlTracker` that owns one of these -- before it builds the gateways that caused
    the adapter package to be loaded. A snapshot taken in `__init__` would be empty
    for the whole run, and every fee would be zero.
    """

    def calculate(self, market: Market, notional: float, order_type: OrderType) -> float:
        schedule = get(market)
        if schedule is None:
            self._warn_once(market)
            return 0.0
        return notional * schedule.rate(order_type)

    def _warn_once(self, market: Market) -> None:
        if market.id in _WARNED:
            return
        _WARNED.add(market.id)
        known = ", ".join(registered_markets()) or "none"
        LOG.warning(
            f"No fee schedule registered for {market.id}: its fills are being costed at zero "
            f"and this run's PnL omits them. Registered: {known}. "
            f"An adapter package registers the venues it adapts; "
            f"qate.trading.fee.register_rates is how anything else does."
        )
