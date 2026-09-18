from __future__ import annotations

import os
import re
from pathlib import Path

from .models import Repository
from .text import normalize

_MARKERS = (".git", "pyproject.toml", "package.json", "Cargo.toml", "go.mod", "pom.xml", "build.gradle")
_LANGUAGE_EXTENSIONS = {
    ".py": "python", ".pyi": "python", ".js": "javascript", ".jsx": "javascript",
    ".ts": "typescript", ".tsx": "typescript", ".go": "go", ".rs": "rust",
    ".java": "java", ".kt": "kotlin", ".kts": "kotlin", ".php": "php",
    ".rb": "ruby", ".cs": "csharp", ".c": "c", ".h": "c", ".cpp": "cpp",
    ".cc": "cpp", ".hpp": "cpp", ".swift": "swift", ".scala": "scala",
}
_CONTINUATION = re.compile(r"^\s*(continue|continuez|vas[- ]?y|go|ok(?:ay)?(?:\s+continue)?|fais[- ]?le|apply|applique|corrige ça|fix it)\s*[.!?]*\s*$", re.I)


def is_within(path: Path, roots: tuple[Path, ...]) -> bool:
    resolved = path.resolve()
    return any(resolved == root.resolve() or root.resolve() in resolved.parents for root in roots)


def find_project_root(start: Path, workspace_roots: tuple[Path, ...] = ()) -> Path | None:
    current = start.resolve()
    if current.is_file():
        current = current.parent
    allowed = tuple(root.resolve() for root in workspace_roots)
    for candidate in (current, *current.parents):
        if allowed and not is_within(candidate, allowed):
            continue
        if any((candidate / marker).exists() for marker in _MARKERS):
            return candidate
    return None


def _description(root: Path) -> str:
    for name in ("README.md", "README.rst", "README.txt"):
        path = root / name
        if not path.is_file():
            continue
        try:
            resolved = path.resolve(strict=True)
            resolved.relative_to(root.resolve())
        except (OSError, ValueError):
            continue
        if path.is_symlink():
            continue
        try:
            lines = resolved.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        meaningful = [line.strip().lstrip("#").strip() for line in lines[:40] if line.strip()]
        return " — ".join(meaningful[:3])[:600]
    return ""


def _languages(root: Path, sample_limit: int = 2000) -> tuple[str, ...]:
    counts: dict[str, int] = {}
    seen = 0
    for directory, names, files in os.walk(root):
        names[:] = [name for name in names if name not in {".git", ".venv", "node_modules", "dist", "build", "vendor", "target"}]
        for filename in files:
            language = _LANGUAGE_EXTENSIONS.get(Path(filename).suffix.lower())
            if language:
                counts[language] = counts.get(language, 0) + 1
            seen += 1
            if seen >= sample_limit:
                break
        if seen >= sample_limit:
            break
    return tuple(name for name, _ in sorted(counts.items(), key=lambda item: (-item[1], item[0])))


def discover_repositories(workspace_roots: tuple[Path, ...], aliases: dict[str, Path] | None = None, max_depth: int = 4) -> list[Repository]:
    aliases = aliases or {}
    found: dict[Path, set[str]] = {}
    for workspace in workspace_roots:
        workspace = workspace.resolve()
        if not workspace.is_dir():
            continue
        direct = find_project_root(workspace, (workspace,))
        if direct == workspace:
            found.setdefault(workspace, set())
        for directory, names, _ in os.walk(workspace):
            current = Path(directory)
            try:
                depth = len(current.relative_to(workspace).parts)
            except ValueError:
                continue
            names[:] = [name for name in names if name not in {".git", ".venv", "node_modules", "dist", "build", "vendor", "target"}]
            if depth > max_depth:
                names[:] = []
                continue
            if any((current / marker).exists() for marker in _MARKERS):
                found.setdefault(current.resolve(), set())
                names[:] = []
    for alias, path in aliases.items():
        root = find_project_root(path, workspace_roots)
        if root and is_within(root, workspace_roots):
            found.setdefault(root, set()).add(alias.lower())
    repositories = []
    for root, extra_aliases in sorted(found.items(), key=lambda item: item[0].as_posix()):
        name = root.name
        generated = {name.lower(), normalize(name).replace("-", " ").replace("_", " ")}
        repositories.append(Repository(root, name, tuple(sorted(generated | extra_aliases)), _description(root), _languages(root)))
    return repositories


def _match_in_text(text: str, repositories: list[Repository]) -> Repository | None:
    lowered = normalize(text).replace("-", " ").replace("_", " ")
    matches: list[tuple[int, Repository]] = []
    for repository in repositories:
        for alias in repository.aliases:
            normalized_alias = normalize(alias).replace("-", " ").replace("_", " ")
            if normalized_alias and re.search(rf"(?<![a-z0-9]){re.escape(normalized_alias)}(?![a-z0-9])", lowered):
                matches.append((len(normalized_alias), repository))
    if not matches:
        return None
    matches.sort(key=lambda item: (-item[0], item[1].name))
    best_length = matches[0][0]
    best = {item[1].root: item[1] for item in matches if item[0] == best_length}
    return next(iter(best.values())) if len(best) == 1 else None


def resolve_repository(query: str, cwd: Path, repositories: list[Repository], recent_user_messages: tuple[str, ...] = ()) -> tuple[Repository | None, str]:
    explicit = _match_in_text(query, repositories)
    if explicit:
        return explicit, "explicit-query"
    current_root = find_project_root(cwd, tuple(repository.root for repository in repositories))
    if current_root:
        current = next((repository for repository in repositories if repository.root == current_root), None)
        if current:
            return current, "current-working-directory"
    if _CONTINUATION.fullmatch(query.strip()):
        for message in reversed(recent_user_messages[-3:]):
            previous = _match_in_text(message, repositories)
            if previous:
                return previous, "continuation-user-history"
    return None, "unresolved"
