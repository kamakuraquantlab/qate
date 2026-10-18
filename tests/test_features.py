import pytest

from qate.core import features
from qate.trading.exceptions import StopTradingException
from qate.trading.inventory import DISABLE_INVENTORY_HEALTH_CHECK, Inventory


@pytest.fixture(autouse=True)
def empty_features(monkeypatch):
    monkeypatch.setattr(features, "_ENABLED", frozenset())


def test_features_are_disabled_by_default():
    assert not features.get("SOME_FEATURE")
    assert not features.get(DISABLE_INVENTORY_HEALTH_CHECK)


def test_presence_enables_a_feature():
    features.load_from_list(["FIRST", "SECOND"])

    assert features.get("FIRST")
    assert features.get("SECOND")
    assert not features.get("THIRD")


def test_loading_replaces_the_process_feature_set():
    features.load_from_list(["OLD"])
    features.load_from_list(["NEW"])

    assert not features.get("OLD")
    assert features.get("NEW")


@pytest.mark.parametrize("configured", [{"OLD_FORMAT": True}, ["VALID", 1]])
def test_feature_configuration_must_be_a_list_of_strings(configured):
    with pytest.raises(TypeError, match="list of strings"):
        features.load_from_list(configured)


def test_inventory_health_check_is_enabled_unless_explicitly_disabled():
    inventory = Inventory.__new__(Inventory)
    inventory.tracker = type("Tracker", (), {"net_realized": -2.0})()
    inventory._peak_pnl = 0.0
    inventory.max_drawdown = 1.0

    with pytest.raises(StopTradingException):
        inventory._health_check()

    features.load_from_list([DISABLE_INVENTORY_HEALTH_CHECK])
    inventory._health_check()
