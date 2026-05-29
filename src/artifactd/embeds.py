from __future__ import annotations

import re
from collections.abc import Iterable
from urllib.parse import parse_qsl, urlencode, urlsplit

_CARD_PATH_RE = re.compile(r"^/_embed/([a-z0-9](?:[a-z0-9-]*[a-z0-9])?)$")
_SHARE_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def canonical_artifact_card_path(value: str, *, allowed_hosts: Iterable[str]) -> str | None:
    """Return the canonical artifact-card path for an allowed card URL.

    The tldraw embed surface is intentionally narrow: only same/allowed-host
    `/_embed/{slug}` card URLs are accepted, with an optional `share` token.
    Full artifact URLs, external hosts, fragments, nested paths, and arbitrary
    query parameters are rejected instead of being converted into iframes.
    """

    raw = str(value or "").strip()
    if not raw:
        return None

    try:
        parsed = urlsplit(raw)
    except ValueError:
        return None

    if parsed.scheme:
        if parsed.scheme not in {"http", "https"}:
            return None
        allowed = {host.strip().lower() for host in allowed_hosts if host and host.strip()}
        if parsed.netloc.lower() not in allowed:
            return None
    elif parsed.netloc:
        return None

    if parsed.fragment:
        return None

    match = _CARD_PATH_RE.fullmatch(parsed.path)
    if not match:
        return None

    pairs = parse_qsl(parsed.query, keep_blank_values=True)
    if not pairs:
        return parsed.path
    if len(pairs) != 1:
        return None
    key, token = pairs[0]
    if key != "share" or not _SHARE_TOKEN_RE.fullmatch(token):
        return None
    return f"{parsed.path}?{urlencode({'share': token})}"


def is_canonical_artifact_card_slug(slug: str) -> bool:
    return canonical_artifact_card_path(f"/_embed/{slug}", allowed_hosts=()) == f"/_embed/{slug}"
