"""Small pieces with no trading opinion in them.

`dt_range` is the one to know: `DtRange` parses the date and datetime formats
the tools accept and yields the ISO date partition keys that both the bronze
layer and `qate.store.timeseries` are laid out by, so a range maps onto files
without anyone converting anything.

`logging.set_now` makes log records carry the replayed clock rather than wall
time, which is what lets a backtest's log be read against the market it was
replaying.
"""
