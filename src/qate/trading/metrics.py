from qate.core.ev_type import EventType
from qate.core.feed import StatusFeed
from qate.core.model import Metric
from qate.util.buffer import BufferedWriter


class FeedWriter(BufferedWriter):
    def __init__(self, status_feed: StatusFeed, threshold: int = 64):
        super(FeedWriter, self).__init__(threshold)
        self.status_feed = status_feed

    def write(self, buffer: list[Metric]) -> None:
        self.status_feed.publish_status(EventType.METRICS, buffer)
