"""Create a portable workspace without touching existing configuration."""

from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from tzlocal import get_localzone_name

from tdl_bot.core.errors import UserError
from tdl_bot.core.i18n import tr


def system_timezone():
    """Detect an IANA name for container configuration on the host."""
    try:
        name = get_localzone_name()
        if name:
            ZoneInfo(name)
            return name
    except (OSError, ValueError, ZoneInfoNotFoundError):
        pass
    return ""


def initialize(directory, *, announce=True):
    root = directory.expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    config = root / ".env"
    template = Path(__file__).parents[1] / "resources" / "env.example"
    content = template.read_text(encoding="utf-8").replace(
        "TZ=\n", f"TZ={system_timezone()}\n"
    )
    try:
        with config.open("x", encoding="utf-8") as output:
            output.write(content)
    except FileExistsError as exc:
        raise UserError(
            tr("Configuration already exists: {path}", path=config)
        ) from exc
    config.chmod(0o600)
    for name in ("data", "bin"):
        (root / name).mkdir(exist_ok=True, mode=0o700)
    (root / "data" / "tdl").mkdir(exist_ok=True, mode=0o700)
    if announce:
        print(
            tr(
                "Workspace created: {path}. Edit .env, then run login.",
                path=root,
            )
        )
