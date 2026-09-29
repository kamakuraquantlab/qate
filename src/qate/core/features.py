from enum import Enum
from logging import getLogger
from typing import Any

LOG = getLogger(__name__)


class Feature(Enum):
    INVENTORY_HEALTH_CHECK = True


class FeatureFlags:
    def __init__(self):
        self._flags: dict[Feature, bool] = {}

    def load_from_dict(self, data: dict[str, Any] | None):
        if not data:
            return
        for key, value in data.items():
            feature = self._resolve_feature(key)
            if feature:
                enabled = bool(value)
                self._flags[feature] = enabled
                LOG.info(f"Feature {feature.name}: {enabled}")

    def get(self, feature: Feature):
        return self._flags.get(feature, feature.value)

    def _resolve_feature(self, key: str) -> Feature | None:
        if not key:
            return None
        normalized = key.strip().upper()
        for feature in Feature:
            if feature.name == normalized or feature.value.upper() == normalized:
                return feature
        return None


FEATURES = FeatureFlags()
