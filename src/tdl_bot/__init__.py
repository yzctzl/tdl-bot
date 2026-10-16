"""Telegram media-group downloader bot."""


def main() -> None:
    """Run the command-line entry point."""
    import sys

    if sys.platform == "win32":
        raise SystemExit(
            "Native Windows is not supported yet. Use Docker Desktop "
            "with Linux containers or run inside WSL2."
        )
    from tdl_bot.runtime.cli import main as cli_main

    cli_main()
