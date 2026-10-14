import dataclasses
import json
from enum import Enum


class Encoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, Enum):
            return obj.name
        if hasattr(obj, "to_str") and callable(obj.to_str):
            return obj.to_str()
        attrs = getattr(obj, "__dict__", None)
        if attrs is None and dataclasses.is_dataclass(obj):  # slotted: no __dict__
            attrs = {f.name: getattr(obj, f.name) for f in dataclasses.fields(obj)}
        if attrs:
            return attrs
        return super().default(obj)
