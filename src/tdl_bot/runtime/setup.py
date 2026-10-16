"""Create a portable workspace without touching existing configuration."""

from pathlib import Path

from tdl_bot.core.errors import UserError
from tdl_bot.core.i18n import tr


def initialize(directory):
    root = directory.expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    config = root / ".env"
    template = Path(__file__).parents[1] / "resources" / "env.example"
    try:
        with config.open("x", encoding="utf-8") as output:
            output.write(template.read_text(encoding="utf-8"))
    except FileExistsError as exc:
        raise UserError(
            tr("Configuration already exists: {path}", path=config)
        ) from exc
    config.chmod(0o600)
    for name in ("data", "bin"):
        (root / name).mkdir(exist_ok=True, mode=0o700)
    (root / "data" / "tdl").mkdir(exist_ok=True, mode=0o700)
    print(
        tr("Workspace created: {path}. Edit .env, then run login.", path=root)
    )
