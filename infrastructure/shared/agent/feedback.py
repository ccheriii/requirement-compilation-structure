"""Keep real failure evidence without carrying successful regression output."""

import re


# Terminal colour/cursor sequences from test runners carry no information for
# the model but cost tokens on every repair round.
ANSI_ESCAPE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")


class CheckFailure(ValueError):
    def __init__(self, message, report):
        self.feedback = report
        self.headline = message
        super().__init__(message + ': ' + str(report))


def failure_message(error, limit=2000):
    """Return a short failure headline for wrappers that attach the full report separately.

    str(CheckFailure) embeds the complete report repr (including a JSON copy of the
    same report). Wrapping that string next to the report itself sent the evidence
    to the model two or three times per repair round.
    """
    headline = getattr(error, "headline", None)
    message = strip_ansi(headline if isinstance(headline, str) else str(error))
    if len(message) > limit:
        message = message[:limit] + f"\n... ({len(message) - limit} more characters; see attached check report)"
    return message


def strip_ansi(text):
    return ANSI_ESCAPE.sub("", text) if isinstance(text, str) and "\x1b" in text else text


def clean_feedback(item):
    """Remove terminal escape codes from every string in a feedback structure."""
    if isinstance(item, str):
        return strip_ansi(item)
    if isinstance(item, dict):
        return {key: clean_feedback(child) for key, child in item.items()}
    if isinstance(item, (list, tuple)):
        return [clean_feedback(child) for child in item]
    return item


def compact_feedback(item):
    if isinstance(item, dict):
        if item.get('returncode') == 0:
            return {'returncode': 0}
        return {key: compact_feedback(child) for key, child in item.items()
            if not (key in {'output', 'stdout', 'files'} and item.get('failures'))}
    if isinstance(item, list):
        return [compact_feedback(child) for child in item]
    return strip_ansi(item)
