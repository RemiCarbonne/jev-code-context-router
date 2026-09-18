from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path


DEFAULT_EXCLUDED_PARTS = frozenset({
    ".git", ".hg", ".svn", ".venv", "venv", "node_modules", "__pycache__",
    "dist", "build", "coverage", ".mypy_cache", ".pytest_cache", ".next",
    ".nuxt", "target", "vendor", ".terraform", ".idea", ".vscode",
})
DEFAULT_EXCLUDED_NAME = re.compile(
    r"(^\.env(?:\.|$)|^(?:credentials?|secrets?|tokens?|auth)(?:\.(?:json|ya?ml|toml|ini|cfg))?$|"
    r"^id_(?:rsa|ed25519)(?:\.pub)?$|\.(?:pem|key|p12|pfx)$|"
    r"(?:\.min|\.generated|_pb2)\.[^.]+$|\.lock$)", re.I
)


@dataclass(frozen=True)
class PathPolicy:
    max_file_bytes: int = 300_000
    excluded_parts: frozenset[str] = DEFAULT_EXCLUDED_PARTS
    excluded_name: re.Pattern[str] = DEFAULT_EXCLUDED_NAME

    def allows(self, root: Path, path: Path) -> bool:
        try:
            root = root.resolve()
            resolved = path.resolve(strict=True)
            relative = resolved.relative_to(root)
        except (OSError, ValueError):
            return False
        if not resolved.is_file() or any(part in self.excluded_parts for part in relative.parts):
            return False
        if self.excluded_name.search(relative.name):
            return False
        try:
            return resolved.stat().st_size <= self.max_file_bytes
        except OSError:
            return False
