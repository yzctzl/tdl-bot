"""Discover this user's supervisors without storing credentials."""

import hashlib
import json
import os
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from tempfile import NamedTemporaryFile

from tdl_bot.core.config import Settings, runtime_directory


@dataclass(frozen=True)
class Instance:
    env_file: Path
    runtime_dir: Path


def registry_directory():
    state = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state"))
    return state / "tdl-bot/instances"


@contextmanager
def registered(settings):
    """Keep a private runtime record for the supervisor lifetime."""
    if settings.env_file is None:
        yield
        return
    runtime = runtime_directory(settings).resolve()
    root = registry_directory()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    name = hashlib.sha256(os.fsencode(runtime)).hexdigest()
    record = root / f"{name}.json"
    temporary = None
    try:
        with NamedTemporaryFile(mode="w", dir=root, delete=False) as output:
            temporary = Path(output.name)
            json.dump(
                {
                    "env_file": str(settings.env_file.resolve()),
                    "runtime_dir": str(runtime),
                    "pid": os.getpid(),
                },
                output,
            )
        temporary.chmod(0o600)
        temporary.replace(record)
        yield
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        record.unlink(missing_ok=True)


def discover(is_running):
    """Ignore stale records and discover local pre-registry processes."""
    found = {}
    for record in sorted(registry_directory().glob("*.json")):
        try:
            data = json.loads(record.read_text())
            runtime = Path(data["runtime_dir"])
            config = Path(data["env_file"])
            if not runtime.is_absolute() or not config.is_absolute():
                continue
            if int((runtime / "frontend.pid").read_text()) != data["pid"]:
                continue
            if is_running(runtime):
                found[runtime] = Instance(config, runtime)
        except (OSError, ValueError, KeyError, TypeError):
            continue
    # Limit migration discovery to the current directory and immediate children.
    for config in [Path.cwd() / ".env", *sorted(Path.cwd().glob("*/.env"))]:
        if not config.is_file():
            continue
        try:
            settings = Settings.from_env(login_only=True, env_file=config)
            runtime = runtime_directory(settings).resolve()
            if (
                runtime not in found
                and runtime.is_dir()
                and is_running(runtime)
            ):
                found[runtime] = Instance(config.resolve(), runtime)
        except (OSError, ValueError):
            continue
    return sorted(found.values(), key=lambda instance: str(instance.env_file))
