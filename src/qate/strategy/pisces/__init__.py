"""pisces — cross-exchange arbitrage, and what running one actually involves.

Post a limit order on the maker venue priced ahead of the taker's spread; when it
fills, hedge immediately with a market order on the taker. The profit is the
spread between two venues net of fees, which a maker rebate is usually what tips
positive.

The trading idea is about thirty lines. The rest — and it is most of the module —
is the part that examples usually skip:

| File | Holds |
|---|---|
| `v1.py` | One pair. The clearest read |
| `v2.py` | Several pairs sharing one JPY pool and one PnL tracker |
| `adjust_inventory.py` | Startup reconciliation: what you hold vs what the strategy assumed |
| `source.py` | Maker and taker order placement, against a reserved inventory |
| `schedule.py` | A venue's maintenance window |
| `config.py` | What a run is, as opposed to what a sweep varies |

`adjust_inventory.py` being the largest file is the lesson. Exchanges have no
sub-accounts, so a strategy shares a balance with everything else in the account
and cannot assume the state it left behind. It fetches real balances, works out
whether it can start, and writes a rebalance plan for `corvus` when it cannot.

Read `knowledge/pisces.md` for why each of those decisions is the way it is.
"""
