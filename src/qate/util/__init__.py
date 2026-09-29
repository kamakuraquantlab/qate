"""Small pieces with no trading opinion in them.

`dt_range` is the one to know: `DtRange` parses the date and datetime formats
the tools accept and yields the ISO date partition keys the bronze layer is laid
out by, so a range maps onto files without anyone converting anything.

`counter` accumulates running statistics, `encoder` is the JSON encoder the env
files are written with.
"""
