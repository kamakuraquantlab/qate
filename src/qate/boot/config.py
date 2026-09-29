from abc import ABC, abstractmethod
from dataclasses import dataclass

from qate.boot.bootstrap import Bootstrap
from qate.core.conn import PrivateConnection, PublicConnection
from qate.core.feed import ExchangeFeed, MarketDataFeed
from qate.core.gateway import ExchangeGateway
from qate.core.model import ExchangeName, Market
from qate.env import sys_env
from qate.exchange import factory, registry
from qate.simulator.gateway import SimulatorGateway


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
    strategy_module_name: str | None = None
    variant: str = "v0"
    trading_config_key: str = "DEFAULT"
    chat_config_key: str | None = None
    gateway_name: str = GatewayName.SIMULATOR

    @property
    def strategy_name(self):
        return f"{self.strategy_module_name}.{self.variant}"


@dataclass
class ExchangeComponents:
    public_conn: PublicConnection = None
    private_conn: PrivateConnection = None
    market_feed: MarketDataFeed = None
    exchange_feed: ExchangeFeed = None
    gateway: ExchangeGateway = None


class Configurator:
    def __init__(self, config: BootConfig, profile: TradingProfile):
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

        if profile.gateway_name != GatewayName.PROD:
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
