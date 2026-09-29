import json

import dacite
import pytest

from qate.env.env import ENUM_TYPE_HOOKS, Description
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


def test_description_json_roundtrip_stays_env_name():
    # desc.json written before the enum->str migration holds a plain string;
    # it must deserialize back into an EnvName via the dacite hook.
    desc = Description(EnvName("MEGATRON"), {"config": "builtins.dict"})
    raw = json.loads(json.dumps(desc, cls=Encoder))
    assert raw["name"] == "MEGATRON"

    back = dacite.from_dict(
        data_class=Description,
        data=raw,
        config=dacite.Config(type_hooks=ENUM_TYPE_HOOKS),
    )
    assert back.name == EnvName("MEGATRON")
    assert isinstance(back.name, EnvName)
