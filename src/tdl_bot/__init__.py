"""Telegram media-group downloader bot."""


def main() -> None:
    """Run the command-line entry point."""
    from tdl_bot.runtime.cli import main as cli_main

    cli_main()
