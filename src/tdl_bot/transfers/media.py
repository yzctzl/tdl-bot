"""Media identity and complete-download verification."""

import hashlib
import json
from pathlib import Path

from tdl_bot.core.errors import UserError
from tdl_bot.core.i18n import tr


def _media_fingerprint(messages) -> str | None:
    """Reuse identical Telegram media only when captions/entities also match."""
    parts = []
    for message in messages:
        media = getattr(message, "document", None) or getattr(
            message, "photo", None
        )
        media_id = getattr(media, "id", None)
        if media_id is None:
            return None
        entities = [
            entity.to_dict()
            for entity in (message.entities or [])
            if type(entity).__name__ != "MessageEntitySpoiler"
        ]
        parts.append(
            (type(media).__name__, media_id, message.message, entities)
        )
    encoded = json.dumps(parts, sort_keys=True, default=str).encode()
    return "media:" + hashlib.sha256(encoded).hexdigest()


def _downloaded_files(directory: Path, messages) -> list[Path]:
    """Require exactly one complete file per resolved media-group member."""
    files = []
    for message in messages:
        matches = [
            path
            for path in directory.glob(f"{message.id}_*")
            if path.is_file() and not path.is_symlink()
        ]
        if len(matches) != 1 or matches[0].stat().st_size == 0:
            raise UserError(
                tr(
                    "tdl did not download the entire media group. Upload "
                    "stopped and files cleaned up."
                )
            )
        file = matches[0]
        # Document sizes are exact; photos may use different resolutions.
        if message.document and file.stat().st_size != message.document.size:
            raise UserError(
                tr(
                    "File sizes are incomplete. Upload stopped and files "
                    "cleaned up."
                )
            )
        files.append(file)
    return files
