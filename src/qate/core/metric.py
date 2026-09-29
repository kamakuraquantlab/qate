from abc import ABC, abstractmethod

from .ev_type import EventType
from .feed import StatusFeed
from .model import Measurement


def flatten_dict(data: dict) -> list:
    flat = []
    for a, b in data.items():
        flat.append(a)
        flat.append(b)
    return flat


def create_trading_metric(ts: float, fields: dict, tags: dict = None) -> list:
    if tags is None:
        tags = {}
    return [Measurement.TRADING.value, ts, flatten_dict(tags), flatten_dict(fields)]


class BufferedWriter(ABC):
    def __init__(self, threshold: int):
        self.threshold = threshold
        self.buffer = []

    def add(self, object):
        self.buffer.append(object)
        if len(self.buffer) >= self.threshold:
            self.flush()

    def add_list(self, objects: list):
        self.buffer.extend(objects)
        if len(self.buffer) >= self.threshold:
            self.flush()

    def flush(self):
        self.write(self.buffer)
        self.buffer = []

    @abstractmethod
    def write(self, buffer: list):
        pass

    def close(self):
        if self.buffer:
            self.flush()


class FeedWriter(BufferedWriter):
    def __init__(self, status_feed: StatusFeed, threshold: int = 64):
        super(FeedWriter, self).__init__(threshold)
        self.status_feed = status_feed

    def write(self, buffer: list):
        self.status_feed.publish_status(EventType.METRICS, buffer)
