import re
from typing import Self

_PATTERN = re.compile(r"^[A-Z_]+$")


class EnvName(str):
    """An environment name: any uppercase [A-Z_] string.

    Formerly an Enum with a fixed member set; now a validated ``str`` subclass so
    new environments can be created without editing this file. Being a real
    ``str`` means it round-trips through JSON, path joins, dict keys and f-strings
    unchanged. Usable directly as an argparse ``type=`` (invalid input raises
    ``ValueError``, which argparse reports).
    """

    def __new__(cls, value: str) -> Self:
        if not _PATTERN.fullmatch(value):
            raise ValueError(f"Invalid EnvName {value!r}: must match [A-Z_]+")
        return super().__new__(cls, value)
