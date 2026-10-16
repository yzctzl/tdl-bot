"""Concise menu copy; interpolate dynamic names and URLs after punctuation."""

import re

from tdl_bot.core.i18n import tr as translate


def tr(message, **values):
    template = str(translate(message))
    # Format the translated template before inserting user-controlled values.
    template = re.sub(r"。(?=\n|$)", "", template).replace("。", "，")
    template = re.sub(r"\.(?=\n|$)", "", template)
    template = template.replace(". ", "; ")
    return template.format(**values) if values else template
