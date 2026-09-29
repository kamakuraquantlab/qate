# Strategy Layout & the Variant Contract

How `qate` finds and constructs a strategy, and how to lay one out.

## 1 Directory structure

A strategy is a package. `qate` ships two of them as worked examples —
`qate.strategy.corvus` and `qate.strategy.pisces` — and yours lives wherever you
like, as long as it is importable:

```
my_strategy/
  config.py          # Config dataclass, extends BootConfig
  v1.py              # Variant 1 — one Variant class per file
  v2.py              # Variant 2
  ...
```

Enoshima adds the directory it was launched from to `sys.path`, so a strategy
package sitting beside where you run it is found without installing anything.

## 2 The Variant class

The loader imports `{strategy_module}.{variant_name}` and looks up `Variant`:

```python
# qate/boot/strategy_loader.py
module = importlib.import_module(module_name + "." + variant_name)
StrategyClass = module.Variant
```

So every variant file contains exactly one class named `Variant`, extending
`Strategy[Config]`:

```python
class Variant(Strategy[Config]):
    def __init__(self, config: Config, params: dict): ...
    def warmup_order_book(self, order_book): ...      # fill history, do not trade
    def handle_order_book(self, order_book): ...      # decide, place orders
    def handle_trade(self, trade): ...
    def handle_order_filled(self, order_response): ...
```

The constructor takes exactly two arguments, and the split between them matters:

- **`config`** — an instance of the `Config` in your `config.py`, loaded from
  `config.json`. This is what makes a run *a different run*: a market, an order
  size, which venues.
- **`params`** — a plain `dict`, loaded from `param_grid.json`. This is what an
  optimize sweep varies.

Put a tunable in `config` and it cannot be swept. Put a market in `params` and
every combination reloads the data. See [02_env.md §5](02_env.md#5-config-discovery) for
how the env loads both.

## 3 The two rules that break a backtest silently

**Time comes from the event.** Set `self.now_ts` from each event's timestamp and
use it everywhere. A strategy that calls `time.time()` still runs and still
produces numbers — it is comparing a wall clock against replayed prices. See
[01_philosophy.md §3](01_philosophy.md#3-never-use-timetime-in-strategies).

**Warmup is not trading.** `warmup_order_book` and `warmup_trade` fill history
until `is_ready` returns True; nothing before that should place an order. Deciding
from a half-full indicator makes the first stretch of every backtest measure
something that is still converging.

## 4 New variant, or edit in place?

**New variant file** for any logic change: a changed entry or exit rule, a new
signal, a different risk approach. The old variant stays on disk and in git
history, so a result can be reproduced and two versions compared. A run log
records `strategy_module.variant`, which is only meaningful if that name still
refers to the same code.

**Edit in place** only for changes that cannot move a number: logging, typos, a
config field rename.

**Duplicate code between variants is fine.** Variant files are deliberately
self-contained. Shared helpers go in `config.py` or a module beside it; do not
refactor variants into each other, because that is how editing v3 silently changes
what v1 meant.

## 5 TradingProfile

`qate.boot.config.TradingProfile` ties an environment to a variant at runtime,
loaded from `trading.json`:

```python
@dataclass
class TradingProfile:
    strategy_module_name: str   # e.g. "qate.strategy.pisces", or "my_strategy"
    variant: str = "v0"         # file name without .py
    gateway_name: str = GatewayName.SIMULATOR
```

`strategy_name` returns `"{module}.{variant}"`, which is what appears in run logs
and in the partition path of a result.

`gateway_name` is `SIMULATOR` or `PROD`. `PROD` needs an exchange adapter
installed; without one it raises, naming what is registered. That is deliberate —
see [../AGENTS.md](../AGENTS.md).

## 6 Worked examples

| Read | For |
|---|---|
| [05_corvus.md](05_corvus.md) | The order lifecycle alone: place, reprice, cancel, fill, stop |
| [04_pisces.md](04_pisces.md) | A real idea, and what startup, inventory and shutdown cost |

Start with corvus. It has no signal, so what remains is the machinery.
