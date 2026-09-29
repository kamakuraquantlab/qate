from qate.core.feed import StatusFeed


class Chat(StatusFeed):
    def __init__(self):
        super().__init__()

    def connect(self):
        pass

    def disconnect(self):
        pass

    def send(self, data):
        pass
