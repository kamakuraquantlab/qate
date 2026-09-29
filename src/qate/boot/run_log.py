import json
import os
import time
import uuid
from logging import getLogger

from qate.env.env import Env
from qate.util.encoder import Encoder

LOG = getLogger(__name__)


class RunLog:
    def __init__(self, env: Env):
        self.env = env
        self.start_ts = time.time()
        self._id = str(uuid.uuid4())

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), sort_keys=True, indent=indent, cls=Encoder)

    def to_dict(self):
        return {
            "id": self._id,
            "start_ts": self.start_ts,
            "git_rev": self.env.git_rev,
            "config": self.env.get("config"),
            "trading": self.env.get("trading"),
            "params": self.env.get("params"),
        }

    def get_id(self) -> str:
        return self._id

    def get_file_path(self, root_dir: str):
        return os.path.join(root_dir, f"{self.get_id()}.json")

    def save_to_file(self, root_dir: str):
        file_path = self.get_file_path(root_dir)
        content = self.to_json()
        LOG.info(f"Save run log to {file_path}")
        LOG.info(f"Run log content: {content}")
        with open(file_path, "w") as f:
            f.write(content)

    def exists(self, root_dir) -> bool:
        return os.path.exists(self.get_file_path(root_dir))

    def get_content(self, root_dir: str):
        file_path = os.path.abspath(self.get_file_path(root_dir))
        with open(file_path, "r") as f:
            result = json.loads(f.read())
            result["file_path"] = file_path
            return result


class RunLogger:
    def __init__(self, root_dir: str = ""):
        self.root_dir = os.path.join(root_dir, "run_log")
        os.makedirs(self.root_dir, exist_ok=True)

    def get(self, run_log: RunLog) -> str:
        if run_log.exists(self.root_dir):
            return run_log.get_content(self.root_dir)
        return None

    def save(self, run_log: RunLog) -> str:
        run_log.save_to_file(self.root_dir)
