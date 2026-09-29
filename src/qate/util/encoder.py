import json
from enum import Enum


class Encoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, Enum):
            return obj.name
        if hasattr(obj, "to_str") and callable(getattr(obj, "to_str")):
            return obj.to_str()
        if obj.__dict__:
            return obj.__dict__
        return super().default(obj)
