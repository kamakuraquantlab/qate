import fcntl
import importlib
import json
import os
import subprocess
import sys
from dataclasses import dataclass, field
from logging import INFO, WARNING, Formatter, getLogger
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path
from typing import Any

import dacite

from qate.core.model import ExchangeName, Market, SettleType, Side, Symbol
from qate.util.encoder import Encoder

from .env_name import EnvName

ENUM_TYPE_HOOKS = {
    ExchangeName: lambda x: ExchangeName[x],
    Symbol: lambda x: Symbol[x],
    Side: lambda x: Side[x],
    SettleType: lambda x: SettleType[x],
    EnvName: lambda x: EnvName(x),
    Market: Market.from_str,
}


LOCK_FILE = ".lock"
DESCRIPTION_FILE = "desc.json"


@dataclass
class Description:
    name: EnvName
    modules: dict[str, str] = field(default_factory=dict)


UNKNOWN_GIT_REV = "unknown"


def get_git_rev(short_hash_len: int = 7) -> str:
    """The revision of the installed library, recorded in every run log.

    Empty when there is no checkout to ask -- an install from a wheel has no
    `.git` -- because the revision is provenance for a result, not something a
    run depends on. Failing here would make an installed package unable to
    start an environment at all.
    """
    try:
        full_hash = (
            subprocess.check_output(
                ["git", "rev-parse", "HEAD"],
                cwd=Path(__file__).parent,
                stderr=subprocess.DEVNULL,
            )
            .decode()
            .strip()
        )
    except (subprocess.CalledProcessError, FileNotFoundError, OSError):
        return UNKNOWN_GIT_REV
    return full_hash[:short_hash_len]


class Env:
    """A named directory holding one run's configuration, and its lifecycle.

    `work_dir` normally follows from the root and the name. Passing it explicitly
    separates *where the definition is read from* from *where a run writes*, which
    is what lets a backtest run against a live environment's config files without
    taking that environment's lock or writing into its directory.
    """

    def __init__(
        self,
        root_dir: str,
        env_name: EnvName,
        enable_multiprocess_logging: bool = False,
        work_dir: str | None = None,
    ):
        self.root_dir = root_dir
        self.name = env_name
        self.work_dir = str(work_dir) if work_dir else os.path.join(self.root_dir, self.name)
        self.git_rev = get_git_rev()
        self.hooks = ENUM_TYPE_HOOKS.copy()
        self.objects = {}
        self.enable_multiprocess_logging = enable_multiprocess_logging

    def visit(self):
        os.chdir(self.work_dir)
        self.set_logging()

    def enter(self, daemon: bool = False):
        """
        Enter the environment.

        Args:
            daemon: If True, this is a long-running daemon process.
                   On first run, generates start/stop scripts and exits.
        """
        if daemon:
            self._check_daemon_scripts()

        self.visit()
        self.lock()

    def leave(self):
        self.unlock()

    def _check_daemon_scripts(self):
        """
        Check if start/stop scripts exist for this daemon.
        If not, generate them and exit.
        """
        # Get the Python file being executed
        py_file = sys.argv[0]
        py_file_abs = os.path.abspath(py_file)
        py_file_name = Path(py_file).stem  # filename without extension

        # Script paths in the env folder
        start_script = os.path.join(self.work_dir, f"start_{py_file_name}.sh")
        stop_script = os.path.join(self.work_dir, f"stop_{py_file_name}.sh")

        # Check if scripts already exist
        if os.path.exists(start_script) and os.path.exists(stop_script):
            return  # Scripts exist, continue normally

        # Generate start script
        start_content = f"""#!/bin/bash
# Auto-generated start script for {py_file_name}

cd "$(dirname "$0")"

# Run the Python script as a daemon
nohup {sys.executable} {py_file_abs} {" ".join(sys.argv[1:])} > {py_file_name}.out 2>&1 &

echo "Started {py_file_name} (PID: $!)"
echo "Output: {os.path.join(self.work_dir, py_file_name + ".out")}"
"""

        # Generate stop script
        stop_content = f"""#!/bin/bash
# Auto-generated stop script for {py_file_name}

cd "$(dirname "$0")"

LOCK_FILE=".lock"

if [ ! -f "$LOCK_FILE" ]; then
    echo "Lock file not found. Process may not be running."
    exit 1
fi

# Read PID from lock file
PID=$(cat "$LOCK_FILE")

if [ -z "$PID" ]; then
    echo "Could not read PID from lock file"
    exit 1
fi

echo "Stopping {py_file_name} (PID: $PID)..."
kill $PID

# Wait for process to stop
for i in {{1..10}}; do
    if ! kill -0 $PID 2>/dev/null; then
        echo "{py_file_name} stopped successfully"
        exit 0
    fi
    sleep 1
done

echo "Warning: Process may still be running"
"""

        # Write start script
        with open(start_script, "w") as f:
            f.write(start_content)
        os.chmod(start_script, 0o755)

        # Write stop script
        with open(stop_script, "w") as f:
            f.write(stop_content)
        os.chmod(stop_script, 0o755)

        # Print information and exit
        print("\n" + "=" * 80)
        print("DAEMON SCRIPTS GENERATED")
        print("=" * 80)
        print(f"\nThis is the first time running {py_file_name} as a daemon.")
        print("Start and stop scripts have been generated in the environment folder.\n")
        print(f"Start script: {start_script}")
        print(f"Stop script:  {stop_script}\n")
        print("To start the daemon, run:")
        print(f"  {start_script}\n")
        print("To stop the daemon, run:")
        print(f"  {stop_script}\n")
        print("=" * 80)
        sys.exit(0)

    def get(self, key: str) -> Any:
        return self.objects.get(key, None)

    def load(self):
        if not os.path.isdir(self.work_dir):
            raise Exception(f"{self.name} not setup")

        self.desc: Description = self.load_object(DESCRIPTION_FILE, Description)
        if self.desc.name != self.name:
            raise Exception(f"Inconsistent Profile {self.name}")

        for key, class_name in self.desc.modules.items():
            module_name, class_name = class_name.rsplit(".", 1)
            mod = importlib.import_module(module_name)
            cls = None if module_name == "builtins" else getattr(mod, class_name)
            self.objects[key] = self.load_object(key + ".json", cls)

    def load_object(self, file_name: str, object_cls=None) -> Any:
        file = os.path.join(self.work_dir, file_name)
        if not os.path.isfile(file):
            return None
        with open(file, "r", encoding="utf-8") as f:
            raw_data = json.load(f)
        if not object_cls:
            return raw_data
        return dacite.from_dict(
            data_class=object_cls,
            data=raw_data,
            config=dacite.Config(type_hooks=self.hooks),
        )

    def save_object(self, file_name: str, object: Any, forced=False) -> str:
        file = os.path.join(self.work_dir, file_name)
        if os.path.isfile(file):
            if not forced:
                raise Exception(f"{file} exists")
        with open(file, "w", encoding="utf-8") as f:
            json.dump(object, f, cls=Encoder, indent=2, sort_keys=True)
        return file

    def lock(self):
        lock_file = os.path.join(self.work_dir, LOCK_FILE)
        lock_fd = open(lock_file, "a")

        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except IOError:
            # Failed to acquire lock, close and exit without modifying anything
            lock_fd.close()
            raise RuntimeError("Another process is already running.")

        # Successfully acquired lock, truncate and write PID
        lock_fd.seek(0)
        lock_fd.truncate()
        lock_fd.write(str(os.getpid()))
        lock_fd.flush()
        self.lock_fd = lock_fd

    def unlock(self):
        if hasattr(self, "lock_fd") and self.lock_fd:
            try:
                # Release the lock
                fcntl.flock(self.lock_fd, fcntl.LOCK_UN)
                self.lock_fd.close()
            except Exception:
                pass

            lock_file = os.path.join(self.work_dir, LOCK_FILE)
            if os.path.exists(lock_file):
                try:
                    os.remove(lock_file)
                except Exception:
                    pass

    def add_enum_to_hooks(self, enum):
        self.hooks[enum] = lambda x: enum[x]

    def set_logging(self):
        root = getLogger()
        filename = "env.log"
        if self.enable_multiprocess_logging:
            filename = f"env_{os.getpid()}.log"
        handler = TimedRotatingFileHandler(
            filename,
            when="midnight",
            interval=1,
            encoding="utf-8",
        )
        formatter = Formatter("{asctime} {levelname:8s} {name} {message}", style="{")
        handler.setFormatter(formatter)
        root.addHandler(handler)
        root.setLevel(INFO)
        getLogger("httpx").setLevel(WARNING)

    def setup(self, desc: Description, forced=False):
        if not os.path.isdir(self.work_dir):
            os.makedirs(self.work_dir)
        else:
            if not forced:
                raise Exception(f"{self.work_dir} is not empty")
        self.save_object(DESCRIPTION_FILE, desc, forced)

    @classmethod
    def setup_env(
        cls,
        root_dir: str,
        env_name: EnvName,
        objects: dict[str, Any] = None,
    ) -> "Env":
        env = cls(root_dir, env_name)
        modules = {}

        if objects:
            for name, obj in objects.items():
                modules[name] = obj.__class__.__module__ + "." + obj.__class__.__name__

        desc = Description(
            env.name,
            modules,
        )
        env.setup(desc)

        if objects:
            for name, obj in objects.items():
                env.save_object(name + ".json", obj)

        return env

    @classmethod
    def load_env(cls, root_dir: str, env_name: EnvName) -> "Env":
        env = Env(root_dir, env_name)
        env.load()
        return env
