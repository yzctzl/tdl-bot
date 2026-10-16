"""Display stored UTC timestamps in the running machine's local timezone."""

from datetime import UTC, datetime


def local_timestamp(timestamp: float) -> str:
    # Convert the recorded instant so historical DST uses the correct offset.
    return (
        datetime.fromtimestamp(timestamp, UTC)
        .astimezone()
        .strftime("%Y-%m-%d %H:%M %Z")
    )
