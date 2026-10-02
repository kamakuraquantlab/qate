"""What a fill costs: the mechanism here, the numbers from whoever knows them.

A fee rate is a venue's fact. It is published on a venue's own fee page, changes
when the venue decides, and differs per account tier -- so a table of rates in a
library is a table that is wrong somewhere and silently. `qate` therefore holds no
rate at all. It holds the shape of one, a registry to put them in, and the
arithmetic that turns a rate into a cost.

`qate-exchanges` registers the venues it adapts, through an entry point, which is
the same argument and the same mechanism as `qate.exchange.registry`: the package
that speaks to a venue is the package that tracks what that venue charges.

## Where the rates come from

A package supplying rates declares an entry point in the `qate.fees` group:

```toml
[project.entry-points."qate.fees"]
my_venues = "my_venues.fees:register_fees"
```

The named object is called with no arguments on the first fee lookup, and is expected
to `register` once per market it knows. Loading is lazy and happens once, so an
installed package is enough -- nothing has to import it, and nothing has to be
imported in the right order. `QATE_FEE_PLUGINS` overrides discovery with a
comma-separated list of `module` or `module:attr` targets, for a checkout that is not
installed.

This is a *separate* group from `qate.exchanges`, and deliberately so. A replay needs
to know what a fill costs and must still never ask for a venue, so the entry point
above should name a module holding rates and nothing else -- no venue module, no http
or websocket client, no connection of any kind. Resolving a fee then imports that
module (and its package, as any import does) without touching
`qate.exchange.registry`, which is what keeps the backtest tripwire in
`test_backtest_end_to_end.py` meaningful: discovering a rate is not asking for a
venue.

## Registering by hand

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
A process with no fee plugin installed computes **zero fees**, and a strategy whose
edge is thinner than its fees will look profitable. Fees were hardcoded here once, so
this could not happen; the cost of their being right is that something now has to
supply them. `registered_markets()` is there to be asserted on before a run that
cares.
"""

from dataclasses import dataclass
from logging import getLogger

from qate.core.model import ExchangeName, Market
from qate.core.order import OrderType
from qate.core.symbol import Symbol
from qate.util.plugins import load_plugins

LOG = getLogger(__name__)

ENTRY_POINT_GROUP = "qate.fees"
PLUGIN_ENV_VAR = "QATE_FEE_PLUGINS"


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
_discovered = False


def register(market: Market, schedule: FeeSchedule) -> None:
    """Record what one market charges. A second registration replaces the first.

    Replacing is deliberate and not warned about, unlike an adapter: an account tier,
    a campaign or a run's own assumption is a legitimate reason to override what a
    plugin registered, and the last word belongs to whoever is closest to the run.

    Which is why this discovers first. Were a plugin's rates to arrive later -- on
    the first lookup, as they do -- they would land on top of the caller's override
    and silently win.
    """
    discover()
    _SCHEDULES[market.id] = schedule
    _WARNED.discard(market.id)


def register_rates(exchange_name: ExchangeName, symbol: Symbol, maker: float, taker: float) -> None:
    """`register` for a caller holding rates rather than a `FeeSchedule`."""
    register(Market(exchange_name, symbol), FeeSchedule(maker=maker, taker=taker))


def get(market: Market) -> FeeSchedule | None:
    """One market's schedule, or `None` if nothing registered it.

    Discovers installed fee plugins on the first call, the way
    `qate.exchange.registry.get` discovers adapters.
    """
    discover()
    return _SCHEDULES.get(market.id)


def registered_markets() -> list[str]:
    """The market ids with a schedule. Assert on this before a run that needs fees."""
    discover()
    return sorted(_SCHEDULES)


def discover(force: bool = False) -> None:
    """Load fee plugins. Idempotent; `force` re-runs it.

    `_discovered` is set before loading, so a plugin calling `register` -- which
    discovers -- re-enters this and returns immediately instead of recursing.
    """
    global _discovered
    if _discovered and not force:
        return
    _discovered = True
    load_plugins(ENTRY_POINT_GROUP, PLUGIN_ENV_VAR, LOG, default_attr="register_fees")


def clear() -> None:
    """Forget every schedule. For tests, and for a process rebuilding its own.

    Does not undo discovery: the next lookup repopulates from whatever is installed.
    A test wanting a registry that stays empty patches `_discovered` as well, the way
    `tests/test_exchange_registry.py` does -- on a developer's machine a plugin
    usually is installed.
    """
    _SCHEDULES.clear()
    _WARNED.clear()


class FeeCalculator:
    """Notional and order type in, cost out.

    It reads the registry on every call rather than snapshotting it at construction,
    which is what lets discovery be lazy. A `PnlTracker` is built with its strategy,
    before anything has asked for a fee and therefore before any plugin has been
    loaded; a snapshot taken in `__init__` would be empty for the whole run and every
    fee would be zero. The first `calculate` is what triggers discovery.
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
            f"Install a package providing the {ENTRY_POINT_GROUP} entry point, "
            f"point {PLUGIN_ENV_VAR} at one, or call qate.trading.fee.register_rates."
        )
