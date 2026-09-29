import json
from dataclasses import dataclass
from itertools import product


@dataclass
class Value:
    name: str
    value: float


def _to_dict(value_list: list[Value]):
    result = {}
    for v in value_list:
        result[v.name] = v.value
    return result


@dataclass
class Spec:
    name: str
    default_value: Value
    values: list[Value]

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "default_value": self.default_value.value,
            "values": [v.value for v in self.values],
        }


@dataclass
class ParamGrid:
    specs: list[Spec]

    def get_default(self) -> dict:
        vals = [spec.default_value for spec in self.specs]
        return _to_dict(vals)

    def get_all(self) -> list[dict]:
        result = []
        combos = product(*(p.values for p in self.specs))
        for combo in combos:
            result.append(_to_dict(combo))
        return result

    def to_object(self) -> list[dict]:
        result = []
        for spec in self.specs:
            result.append(spec.to_dict())
        return result

    def to_json(self) -> str:
        return json.dumps(self.to_object())

    @classmethod
    def from_object(cls, data: list[dict]) -> "ParamGrid":
        specs = []
        for param in data:
            name = param["name"]
            default_value = Value(name, param["default_value"])
            values = [Value(name, v) for v in param["values"]]
            specs.append(Spec(name, default_value, values))
        return ParamGrid(specs)

    @classmethod
    def from_json(cls, json_str: str) -> "ParamGrid":
        """
        Example JSON format:
        [
            {
                "name": "stop_loss",
                "default_value": 0.02,
                "values": [0.01, 0.02, 0.03, 0.05]
            },
            {
                "name": "take_profit",
                "default_value": 0.02,
                "values": [0.02, 0.05, 1.0]
            }
        }
        """
        data = json.loads(json_str)
        return cls.from_object(data)
