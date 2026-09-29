import importlib
from typing import Type

from qate.trading.strategy import Strategy


def get_strategy_class(module_name: str, variant_name: str) -> Type[Strategy]:
    module = importlib.import_module(module_name + "." + variant_name)
    return module.Variant
