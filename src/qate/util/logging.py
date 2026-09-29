import logging
import sys


class AppFilter(logging.Filter):
    def __init__(self):
        super().__init__()
        self.now_ts = None

    def set_now(self, now_ts):
        self.now_ts = now_ts

    def filter(self, record):
        if self.now_ts:
            record.created = self.now_ts
        return True


APP_FILTER = AppFilter()


def setup_logging():
    logging.basicConfig(
        stream=sys.stdout,
        level=logging.INFO,
        format="%(asctime)s %(name)s %(message)s",
    )


def get_logger(name):
    logger = logging.getLogger(name)
    logger.level = logging.INFO
    logger.addFilter(APP_FILTER)
    return logger


def set_now(now_ts):
    APP_FILTER.set_now(now_ts)
