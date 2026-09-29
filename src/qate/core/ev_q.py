from .model import TimeSeriesData


class EventQueue:
    def get(self, timeout=None) -> tuple[str, TimeSeriesData]:
        pass

    def put(self, event: tuple[str, TimeSeriesData]):
        pass
