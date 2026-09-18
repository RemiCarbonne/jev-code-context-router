from __future__ import annotations

import re
import unicodedata

_TOKEN_RE = re.compile(r"[a-zA-Z_][a-zA-Z0-9_]{1,}")


def normalize(value: str) -> str:
    return unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode().lower()


def tokens(value: str) -> set[str]:
    normalized = normalize(value).replace("_", " ").replace("-", " ")
    result = {token for token in _TOKEN_RE.findall(normalized) if len(token) >= 2}
    result.update(token[:5] for token in tuple(result) if len(token) >= 7)
    return result


def text_content(value: object) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(
            str(item.get("text") or "")
            for item in value
            if isinstance(item, dict) and item.get("type") == "text"
        )
    return str(value or "")
