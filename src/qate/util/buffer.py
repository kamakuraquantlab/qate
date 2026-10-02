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
