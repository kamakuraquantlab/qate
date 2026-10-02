"""Small pieces with no trading opinion in them.

`dt_range` is the one to know: `DtRange` parses the date and datetime formats
the tools accept and yields the ISO date partition keys the bronze layer is laid
out by, so a range maps onto files without anyone converting anything.

`buffer.BufferedWriter` is the batching base every writer in the library uses,
`counter` accumulates running statistics, and `encoder` is the JSON encoder the env
files are written with.

`plugins.load_plugins` is how this package asks what is installed. `qate` ships no
venue and no fee rate, so two registries -- `qate.exchange.registry` and
`qate.trading.fee` -- need the same lazy entry-point lookup, and they share one
implementation of it rather than each keeping a copy to drift from.
"""
