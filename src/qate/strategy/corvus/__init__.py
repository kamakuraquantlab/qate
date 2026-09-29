"""corvus — place a list of orders, wait for them all, exit.

The smallest useful strategy: no signal, no inventory, no schedule. It reads
order instructions from a JSONL file, posts a maker order at the touch for each,
reprices one that has drifted, and raises `EventLoopExit` when every instruction
is filled.

Deliberately dumb, and that is its value twice over. As an example, what is left
once the trading idea is removed is exactly the order lifecycle every strategy has
to get right. As a tool, it is what executes a rebalance `pisces` has asked for —
a human reviews the file, then corvus does what it says and nothing else.

`config.Config.from_jsonl` reads the file, splitting anything larger than the
venue's `MAX_ORDER_SIZE` into chunks. Design notes: `knowledge/05_corvus.md`.
"""
