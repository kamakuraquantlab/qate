from logging import getLogger

LOG = getLogger(__name__)

_ENABLED: frozenset[str] = frozenset()


def load_from_list(data: list[str] | None) -> None:
    """Replace the process's enabled features with the names in ``data``.

    Presence in the list enables a feature. Callers should load the
    configuration once, during process setup.
    """
    global _ENABLED
    if data is not None and (not isinstance(data, list) or not all(isinstance(feature, str) for feature in data)):
        raise TypeError("features must be a list of strings")
    _ENABLED = frozenset(data or ())
    for feature in sorted(_ENABLED):
        LOG.info(f"Feature enabled: {feature}")


def get(feature: str) -> bool:
    """Return whether ``feature`` was enabled when configuration was loaded."""
    return feature in _ENABLED
