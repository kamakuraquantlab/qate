"""The adapter contract, and the guarantee that an empty registry stays empty.

The last test here is the one that matters for publication: with nothing
installed, asking for a venue raises rather than reaching one.
"""

import pytest

from qate.core.model import ExchangeName
from qate.exchange import factory, registry


@pytest.fixture(autouse=True)
def empty_registry(monkeypatch):
    """Each test starts with no adapters and no plugin discovery."""
    monkeypatch.setattr(registry, "_ADAPTERS", {})
    monkeypatch.setattr(registry, "_discovered", True)
    monkeypatch.delenv(registry.PLUGIN_ENV_VAR, raising=False)


class FakeApi:
    def __init__(self, api_key, secret):
        self.api_key = api_key
        self.secret = secret
        self.exchange_name = ExchangeName.GMO


def test_unknown_exchange_names_what_is_registered():
    with pytest.raises(registry.UnknownExchange) as exc:
        factory.create_public_connection(ExchangeName.GMO)
    assert "GMO" in str(exc.value)
    assert registry.PLUGIN_ENV_VAR in str(exc.value)


def test_registered_adapter_is_resolved():
    registry.register(
        registry.ExchangeAdapter(
            exchange_name=ExchangeName.GMO,
            create_api=FakeApi,
        )
    )
    api = factory.create_exchange_api(ExchangeName.GMO, "key", "secret")
    assert isinstance(api, FakeApi)
    assert api.api_key == "key"
    assert factory.available_exchanges() == [ExchangeName.GMO]


def test_missing_hook_names_the_capability():
    registry.register(registry.ExchangeAdapter(exchange_name=ExchangeName.BINANCE))
    with pytest.raises(registry.UnsupportedExchangeCapability) as exc:
        factory.create_public_connection(ExchangeName.BINANCE)
    assert "BINANCE" in str(exc.value)
    assert "create_public_connection" in str(exc.value)


def test_gateway_is_built_on_the_adapter_api():
    built = []
    registry.register(
        registry.ExchangeAdapter(
            exchange_name=ExchangeName.GMO,
            create_api=FakeApi,
            create_gateway=lambda api: built.append(api) or "gateway",
        )
    )
    assert factory.create_exchange_gateway(ExchangeName.GMO, "k", "s") == "gateway"
    assert isinstance(built[0], FakeApi)


def test_second_registration_replaces_the_first():
    registry.register(registry.ExchangeAdapter(exchange_name=ExchangeName.GMO, create_api=FakeApi))
    registry.register(registry.ExchangeAdapter(exchange_name=ExchangeName.GMO))
    with pytest.raises(registry.UnsupportedExchangeCapability):
        factory.create_exchange_api(ExchangeName.GMO, "k", "s")


def test_env_var_plugin_target_is_loaded(monkeypatch):
    monkeypatch.setenv(registry.PLUGIN_ENV_VAR, "fake_plugin:register")
    monkeypatch.setattr(registry, "_discovered", False)
    assert ExchangeName.RAKUTEN in registry.registered()


def test_a_broken_plugin_does_not_break_discovery(monkeypatch, caplog):
    monkeypatch.setenv(registry.PLUGIN_ENV_VAR, "no_such_module,fake_plugin:register")
    monkeypatch.setattr(registry, "_discovered", False)
    registry.discover()
    assert ExchangeName.RAKUTEN in registry.registered()


def test_nothing_installed_means_nothing_reachable():
    """The publication guarantee: no adapter, no venue, for every entry point."""
    assert registry.registered() == []
    for call in (
        lambda: factory.create_public_connection(ExchangeName.GMO),
        lambda: factory.create_exchange_api(ExchangeName.GMO, "k", "s"),
        lambda: factory.create_exchange_order_api(ExchangeName.GMO, "k", "s"),
        lambda: factory.create_exchange_gateway(ExchangeName.GMO, "k", "s"),
        lambda: factory.create_private_connection(FakeApi("k", "s")),
    ):
        with pytest.raises(registry.UnknownExchange):
            call()
