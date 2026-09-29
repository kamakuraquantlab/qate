import importlib
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from logging import getLogger
from typing import Type

from qate.boot.bootstrap import Bootstrap
from qate.core.conn import PrivateConnection, PublicConnection
from qate.core.feed import ExchangeFeed, MarketDataFeed
from qate.core.gateway import ExchangeGateway
from qate.core.model import ExchangeName, Market
from qate.env import sys_env
from qate.exchange import factory, registry
from qate.env.env import Env
from qate.simulator.gateway import SimulatorGateway

LOG = getLogger(__name__)

CONFIG_MODULE = "config"
CONFIG_CLASS = "Config"

CONFIG_FILE = "config.json"
TRADING_FILE = "trading.json"
PARAMS_FILE = "params.json"
PARAM_GRID_FILE = "param_grid.json"
FEATURES_FILE = "features.json"


class BootConfig(ABC):
    """What a run trades: which venues, which markets, which event types."""

    @abstractmethod
    def get_exchanges(self) -> list[ExchangeName]:
        pass

    @abstractmethod
    def get_markets(self) -> list[tuple[Market, list[str]]]:
        pass


class GatewayName:
    PROD = "PROD"
    SIMULATOR = "SIMULATOR"


@dataclass
class TradingProfile:
    """Which strategy an environment runs, and under which credentials.

    Deliberately not which *gateway*. That is a property of the process doing the
    running -- a live runner trades, a backtest simulates -- not of the environment,
    and a field that half the readers ignored was a field that went stale: a
    collector env claiming SIMULATOR, a backtest env claiming anything at all.
    `Configurator` takes it as an argument instead.
    """

    strategy_module_name: str | None = None
    variant: str = "v0"
    trading_config_key: str = "DEFAULT"
    reporter_config_key: str | None = None
    """Which credentials a `Reporter` should be built from, if any.

    Named after `Reporter`, not after Discord or chat. The field was
    `chat_config_key` when the only destination was a chat bot; the destination is
    now whatever the runner decides to add, and a name that says "chat" would send
    every reader looking for a chat interface that no longer exists.
    """

    @property
    def strategy_name(self):
        return f"{self.strategy_module_name}.{self.variant}"


@dataclass
class TradingEnv:
    """Everything an environment says about a run, loaded.

    Loaded by convention rather than through `desc.json`'s module map. The map
    named a class per file, which bought nothing -- every reader already knows it
    wants a `TradingProfile` and a dict -- and cost a file that goes stale when
    code moves. The one genuinely unknown class, the strategy's `Config`, is
    resolved from the strategy module, which `trading.json` already names.
    """

    profile: TradingProfile
    config: BootConfig
    params: dict = field(default_factory=dict)
    param_grid: list | None = None
    features: dict | None = None


def resolve_config_class(strategy_module_name: str) -> Type[BootConfig]:
    """A strategy's `Config`, by the convention every strategy already follows.

    `<strategy_module>.config.Config`, which is what `knowledge/03_writing_strategy.md`
    documents and what an environment's `config.json` deserializes into.
    """
    module_name = f"{strategy_module_name}.{CONFIG_MODULE}"
    try:
        module = importlib.import_module(module_name)
    except ModuleNotFoundError as e:
        raise RuntimeError(
            f"Cannot load {module_name}. A strategy package needs a `config` module "
            f"holding a `Config` class; see knowledge/03_writing_strategy.md."
        ) from e
    if not hasattr(module, CONFIG_CLASS):
        raise RuntimeError(f"{module_name} has no {CONFIG_CLASS} class")
    return getattr(module, CONFIG_CLASS)


def load_trading_env(env: Env) -> TradingEnv:
    """Read an environment's configuration files. Nothing is entered or locked."""
    profile: TradingProfile = env.load_object(TRADING_FILE, TradingProfile)
    if profile is None:
        raise RuntimeError(f"No {TRADING_FILE} in {env.work_dir}")
    if not profile.strategy_module_name:
        raise RuntimeError(f"{TRADING_FILE} in {env.work_dir} names no strategy_module_name")

    config = env.load_object(CONFIG_FILE, resolve_config_class(profile.strategy_module_name))
    if config is None:
        raise RuntimeError(f"No {CONFIG_FILE} in {env.work_dir}")

    return TradingEnv(
        profile=profile,
        config=config,
        params=env.load_object(PARAMS_FILE) or {},
        param_grid=env.load_object(PARAM_GRID_FILE),
        features=env.load_object(FEATURES_FILE),
    )


@dataclass
class ExchangeComponents:
    public_conn: PublicConnection = None
    private_conn: PrivateConnection = None
    market_feed: MarketDataFeed = None
    exchange_feed: ExchangeFeed = None
    gateway: ExchangeGateway = None


class Configurator:
    """Wires an environment's markets and venues into a runtime.

    `gateway_name` is the runner's decision, not the environment's: a live runner
    passes PROD, a paper or replay runner leaves it as SIMULATOR.
    """

    def __init__(
        self,
        config: BootConfig,
        profile: TradingProfile,
        gateway_name: str = GatewayName.SIMULATOR,
    ):
        self.gateway_name = gateway_name
        self.exchanges: dict[ExchangeName, ExchangeComponents] = {}
        for market, event_type_list in config.get_markets():
            self.setup_market_data(market, event_type_list)
        for exchange_name in config.get_exchanges():
            self.setup_gateway(exchange_name, profile)

    def setup_market_data(self, market, event_type_list):
        exchange_name = market.exchange_name
        if exchange_name not in self.exchanges:
            public_conn = factory.create_public_connection(exchange_name)
            components = ExchangeComponents()
            components.public_conn = public_conn
            components.market_feed = public_conn
            self.exchanges[exchange_name] = components

        components = self.exchanges.get(exchange_name)
        for event_type in event_type_list:
            components.market_feed.register_symbol_event(market.symbol, event_type)

    def setup_gateway(self, exchange_name: ExchangeName, profile: TradingProfile):
        components = self.exchanges.get(exchange_name)

        if self.gateway_name != GatewayName.PROD:
            gateway = SimulatorGateway(exchange_name)
            gateway.subscribe_market_data_feed(components.market_feed)
            components.gateway = gateway
            return

        (api_key, secret) = sys_env.get_api_keys(exchange_name.name, profile.trading_config_key)
        gateway = factory.create_exchange_gateway(exchange_name, api_key, secret)

        # Whether a venue has an account feed is the adapter's own answer.
        # Enumerating the venues that do here meant this file had to be edited
        # every time one was added, and it is not this file's knowledge to hold.
        if registry.get(exchange_name).create_private_connection:
            private_conn = factory.create_private_connection(gateway.api)
            components.private_conn = private_conn
            components.exchange_feed = private_conn

        if components.exchange_feed:
            gateway.subscribe_exchange_feed(components.exchange_feed)
        components.gateway = gateway

    def configure(self, bootstrap: Bootstrap):
        for components in self.exchanges.values():
            if components.public_conn:
                bootstrap.add_conn(components.public_conn)
            if components.private_conn:
                bootstrap.add_conn(components.private_conn)
            if components.gateway:
                bootstrap.add_gateway(components.gateway)
