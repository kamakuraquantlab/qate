"""Accumulate, then write in batches.

A writer that flushes on a count rather than per item. Used by anything whose
destination is cheaper in batches than per record -- a local file, a database
write api -- so the caller never thinks about batching.

Subclasses implement `write`, which receives the whole buffer and is free to block;
`add` is the hot path and must not.
"""

from abc import ABC, abstractmethod


class BufferedWriter(ABC):
    def __init__(self, threshold: int):
        self.threshold = threshold
        self.buffer = []

    def add(self, item) -> None:
        self.buffer.append(item)
        if len(self.buffer) >= self.threshold:
            self.flush()

    def add_list(self, items: list) -> None:
        self.buffer.extend(items)
        if len(self.buffer) >= self.threshold:
            self.flush()

    def flush(self) -> None:
        self.write(self.buffer)
        self.buffer = []

    @abstractmethod
    def write(self, buffer: list) -> None:
        pass

    def close(self) -> None:
        if self.buffer:
            self.flush()
