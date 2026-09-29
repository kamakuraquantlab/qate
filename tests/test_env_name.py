import json

import dacite
import pytest

from qate.env.env import ENUM_TYPE_HOOKS
from qate.env.env_name import EnvName
from qate.util.encoder import Encoder


@pytest.mark.parametrize("bad", ["", "lower", "Mixed", "HAS9DIGIT", "HAS-DASH", "HAS SPACE"])
def test_rejects_non_upper_underscore(bad):
    with pytest.raises(ValueError):
        EnvName(bad)


def test_is_str_and_path_safe():
    e = EnvName("EXAMPLE_ENV")
    assert isinstance(e, str)
    assert f"~/env/{e}/desc.json" == "~/env/EXAMPLE_ENV/desc.json"


def test_an_env_name_survives_json_and_comes_back_validated():
    """Config files hold plain strings; the dacite hook re-validates on load."""
    from dataclasses import dataclass

    @dataclass
    class Holder:
        name: EnvName

    raw = json.loads(json.dumps(Holder(EnvName("EXAMPLE_ENV")), cls=Encoder))
    assert raw["name"] == "EXAMPLE_ENV"

    back = dacite.from_dict(
        data_class=Holder,
        data=raw,
        config=dacite.Config(type_hooks=ENUM_TYPE_HOOKS),
    )
    assert back.name == EnvName("EXAMPLE_ENV")
    assert isinstance(back.name, EnvName)
