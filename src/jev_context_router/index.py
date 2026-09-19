from __future__ import annotations

import ast
import hashlib
import json
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

from .errors import RoutingLimitExceeded, RoutingTimeout
from .models import Repository, Symbol
from .security import PathPolicy
from .text import tokens

EXTENSION_LANGUAGE = {
    ".py": "python", ".pyi": "python", ".js": "javascript", ".jsx": "javascript",
    ".mjs": "javascript", ".cjs": "javascript", ".ts": "typescript", ".tsx": "typescript",
    ".go": "go", ".rs": "rust", ".java": "java", ".kt": "kotlin", ".kts": "kotlin",
    ".php": "php", ".rb": "ruby", ".cs": "csharp", ".c": "c", ".h": "c",
    ".cpp": "cpp", ".cc": "cpp", ".hpp": "cpp", ".swift": "swift", ".scala": "scala",
}
_CACHE_VERSION = 2


def _symbol_to_dict(symbol: Symbol) -> dict[str, Any]:
    return {
        "id": symbol.id, "path": symbol.path, "name": symbol.name,
        "qualname": symbol.qualname, "kind": symbol.kind, "language": symbol.language,
        "start_line": symbol.start_line, "end_line": symbol.end_line,
        "source": symbol.source, "imports": symbol.imports,
        "calls": sorted(symbol.calls), "references": sorted(symbol.references),
    }


def _symbol_from_dict(raw: dict[str, Any]) -> Symbol:
    return Symbol(
        id=raw["id"], path=raw["path"], name=raw["name"], qualname=raw["qualname"],
        kind=raw["kind"], language=raw["language"], start_line=int(raw["start_line"]),
        end_line=int(raw["end_line"]), source=raw["source"], imports=raw.get("imports", ""),
        calls=frozenset(raw.get("calls", ())), references=frozenset(raw.get("references", ())),
    )


def cache_path_for(repository: Repository, cache_dir: Path) -> Path:
    identity = hashlib.sha256(str(repository.root.resolve()).encode()).hexdigest()[:24]
    return cache_dir / f"{identity}.json"


def _load_cache(path: Path | None) -> dict[str, Any]:
    if path is None or not path.is_file():
        return {"version": _CACHE_VERSION, "files": {}}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if raw.get("version") == _CACHE_VERSION and isinstance(raw.get("files"), dict):
            return raw
    except (OSError, UnicodeError, json.JSONDecodeError, AttributeError):
        pass
    return {"version": _CACHE_VERSION, "files": {}}


def _write_cache(path: Path | None, payload: dict[str, Any]) -> bool:
    if path is None:
        return False
    try:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        temporary.chmod(0o600)
        temporary.replace(path)
        return True
    except OSError:
        return False


@dataclass
class CodeIndex:
    repository: Repository
    symbols: list[Symbol]
    stats: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.by_id = {symbol.id: symbol for symbol in self.symbols}
        self.by_name: dict[str, list[Symbol]] = {}
        for symbol in self.symbols:
            self.by_name.setdefault(symbol.name, []).append(symbol)


class _PythonFacts(ast.NodeVisitor):
    def __init__(self) -> None:
        self.calls: set[str] = set()
        self.references: set[str] = set()

    def visit_Name(self, node: ast.Name) -> Any:
        if isinstance(node.ctx, ast.Load):
            self.references.add(node.id)
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> Any:
        if isinstance(node.func, ast.Name):
            self.calls.add(node.func.id)
        elif isinstance(node.func, ast.Attribute):
            self.calls.add(node.func.attr)
        self.generic_visit(node)


def _python_symbols(root: Path, path: Path, source: str) -> list[Symbol]:
    relative = path.relative_to(root).as_posix()
    try:
        tree = ast.parse(source, filename=relative)
    except SyntaxError:
        return []
    lines = source.splitlines(keepends=True)
    imports = []
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            imports.append("".join(lines[node.lineno - 1 : node.end_lineno]).rstrip())
    import_source = "\n".join(imports)
    result: list[Symbol] = []

    def add(node: ast.AST, name: str, qualname: str, kind: str) -> None:
        facts = _PythonFacts()
        facts.visit(node)
        start, end = int(getattr(node, "lineno")), int(getattr(node, "end_lineno"))
        result.append(Symbol(
            id=f"{relative}::{qualname}", path=relative, name=name, qualname=qualname,
            kind=kind, language="python", start_line=start, end_line=end,
            source="".join(lines[start - 1 : end]).rstrip(), imports=import_source,
            calls=frozenset(facts.calls), references=frozenset(facts.references),
        ))

    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            add(node, node.name, node.name, "function")
        elif isinstance(node, ast.ClassDef):
            add(node, node.name, node.name, "class")
            for child in node.body:
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    add(child, child.name, f"{node.name}.{child.name}", "method")
    return result


_GENERIC_PATTERNS: dict[str, tuple[re.Pattern[str], ...]] = {
    "javascript": (
        re.compile(r"^\s*(?:export\s+(?:default\s+)?)?(?:async\s+)?function\s+([A-Za-z_$][\w$]*)"),
        re.compile(r"^\s*(?:export\s+(?:default\s+)?)?class\s+([A-Za-z_$][\w$]*)"),
        re.compile(r"^\s*(?:export\s+)?(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?(?:\([^)]*\)|[A-Za-z_$][\w$]*)\s*=>"),
    ),
    "typescript": (
        re.compile(r"^\s*(?:export\s+(?:default\s+)?)?(?:async\s+)?function\s+([A-Za-z_$][\w$]*)"),
        re.compile(r"^\s*(?:export\s+(?:default\s+)?)?(?:abstract\s+)?class\s+([A-Za-z_$][\w$]*)"),
        re.compile(r"^\s*(?:export\s+)?(?:interface|type|enum)\s+([A-Za-z_$][\w$]*)"),
        re.compile(r"^\s*(?:export\s+)?(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*(?::[^=]+)?=\s*(?:async\s*)?(?:\([^)]*\)|[A-Za-z_$][\w$]*)\s*=>"),
    ),
    "go": (re.compile(r"^\s*func\s+(?:\([^)]*\)\s*)?([A-Za-z_]\w*)\s*\("), re.compile(r"^\s*type\s+([A-Za-z_]\w*)\s+(?:struct|interface)\b")),
    "rust": (re.compile(r"^\s*(?:pub\s+)?(?:async\s+)?fn\s+([A-Za-z_]\w*)"), re.compile(r"^\s*(?:pub\s+)?(?:struct|enum|trait)\s+([A-Za-z_]\w*)"), re.compile(r"^\s*impl(?:<[^>]+>)?\s+([A-Za-z_]\w*)")),
    "ruby": (re.compile(r"^\s*def\s+(?:self\.)?([A-Za-z_]\w*[!?=]?)"), re.compile(r"^\s*class\s+([A-Za-z_:]\w*(?:::\w+)*)")),
    "php": (re.compile(r"^\s*(?:final\s+|abstract\s+)?class\s+([A-Za-z_]\w*)"), re.compile(r"^\s*(?:public\s+|protected\s+|private\s+|static\s+)*function\s+([A-Za-z_]\w*)")),
}
_C_STYLE = (
    re.compile(r"^\s*(?:public|private|protected|internal|static|final|abstract|sealed|open|data|async|virtual|override|inline|extern|synchronized|native|partial|unsafe|const|constexpr|template(?:<[^>]+>)?\s+)*\s*(?:class|interface|struct|enum|record|trait)\s+([A-Za-z_]\w*)"),
    re.compile(r"^\s*(?:public|private|protected|internal|static|final|async|virtual|override|inline|extern|synchronized|native|unsafe|const|constexpr|[A-Za-z_][\w<>\[\],.?]*)\s+([A-Za-z_]\w*)\s*\([^;]*\)\s*(?:\{|throws\b)"),
)
_IMPORT_RE = re.compile(r"^\s*(?:import|from|export\s+.*from|require\s*\(|use\s+|include|#include|using\s+|package\s+)")


def _balanced_end(lines: list[str], start: int, language: str) -> int:
    if language == "ruby":
        depth = 0
        for index in range(start, min(len(lines), start + 500)):
            stripped = lines[index].strip()
            if re.match(r"^(class|module|def|if|unless|case|begin|do\b)", stripped):
                depth += 1
            if stripped == "end":
                depth -= 1
                if depth <= 0:
                    return index + 1
        return min(len(lines), start + 120)
    opened = False
    depth = 0
    for index in range(start, min(len(lines), start + 600)):
        line = re.sub(r"//.*$|#.*$", "", lines[index])
        for char in line:
            if char == "{":
                depth += 1
                opened = True
            elif char == "}" and opened:
                depth -= 1
        if opened and depth <= 0:
            return index + 1
        if not opened and index > start + 12 and not line.strip():
            return index
    return min(len(lines), start + 160)


def _generic_symbols(root: Path, path: Path, source: str, language: str) -> list[Symbol]:
    relative = path.relative_to(root).as_posix()
    lines = source.splitlines(keepends=True)
    patterns = _GENERIC_PATTERNS.get(language, _C_STYLE)
    imports = "".join(line for line in lines[:120] if _IMPORT_RE.search(line)).strip()
    result: list[Symbol] = []
    for index, line in enumerate(lines):
        match = next((pattern.search(line) for pattern in patterns if pattern.search(line)), None)
        if not match:
            continue
        name = match.group(1)
        end = _balanced_end(lines, index, language)
        body = "".join(lines[index:end]).rstrip()
        body_tokens = tokens(body)
        kind = "class" if re.search(r"\b(class|interface|struct|enum|trait|type)\b", line) else "function"
        result.append(Symbol(
            id=f"{relative}::{name}@{index + 1}", path=relative, name=name, qualname=name,
            kind=kind, language=language, start_line=index + 1, end_line=end,
            source=body, imports=imports, calls=frozenset(), references=frozenset(body_tokens),
        ))
    if not result and source.strip():
        for start in range(0, len(lines), 160):
            end = min(len(lines), start + 200)
            body = "".join(lines[start:end]).rstrip()
            if body:
                result.append(Symbol(
                    id=f"{relative}::module@{start + 1}", path=relative, name=path.stem,
                    qualname=f"module_{start + 1}", kind="module", language=language,
                    start_line=start + 1, end_line=end, source=body, imports=imports,
                    references=frozenset(tokens(body)),
                ))
    return result


def index_repository(
    repository: Repository,
    policy: PathPolicy | None = None,
    *,
    include_paths: Iterable[Path] | None = None,
    deadline: float | None = None,
    max_files: int = 25_000,
    max_symbols: int = 75_000,
    progress: Callable[[dict[str, Any]], None] | None = None,
    cache_path: Path | None = None,
) -> CodeIndex:
    policy = policy or PathPolicy()
    root = repository.root.resolve()
    symbols: list[Symbol] = []
    files_seen = 0
    files_indexed = 0
    bytes_read = 0
    candidate_files: list[str] = []
    use_cache = include_paths is None and cache_path is not None
    cache = _load_cache(cache_path) if use_cache else {"version": _CACHE_VERSION, "files": {}}
    cached_files: dict[str, Any] = cache["files"]
    next_cached_files: dict[str, Any] = {}
    cache_hits = 0
    cache_misses = 0

    def check_deadline() -> None:
        if deadline is not None and time.monotonic() >= deadline:
            raise RoutingTimeout("indexing", "Repository indexing exceeded its configured timeout.")

    def iter_paths() -> Iterable[Path]:
        if include_paths is not None:
            normalized = {
                (path if path.is_absolute() else root / path).resolve()
                for path in include_paths
            }
            yield from sorted(normalized, key=lambda path: path.as_posix())
            return
        # Path.rglob() cannot prune excluded trees. os.walk(topdown=True) lets us
        # remove dependency/build/cache directories before visiting their contents.
        for directory, names, filenames in os.walk(root, topdown=True, followlinks=False):
            check_deadline()
            names[:] = sorted(
                name for name in names
                if name.lower() not in policy.excluded_parts and not (Path(directory) / name).is_symlink()
            )
            for filename in sorted(filenames):
                yield Path(directory) / filename

    for path in iter_paths():
        check_deadline()
        language = EXTENSION_LANGUAGE.get(path.suffix.lower())
        if not language:
            continue
        files_seen += 1
        if files_seen > max_files:
            raise RoutingLimitExceeded(
                "indexing", f"Repository contains more than {max_files} supported source files."
            )
        if not policy.allows(root, path):
            continue
        relative = path.relative_to(root).as_posix()
        try:
            stat = path.stat()
        except OSError:
            continue
        fingerprint = [stat.st_mtime_ns, stat.st_ctime_ns, stat.st_size]
        cached = cached_files.get(relative) if use_cache else None
        if cached and cached.get("fingerprint") == fingerprint:
            try:
                extracted = [_symbol_from_dict(item) for item in cached.get("symbols", ())]
            except (KeyError, TypeError, ValueError):
                cached = None
            else:
                cache_hits += 1
                symbols.extend(extracted)
                next_cached_files[relative] = cached
                files_indexed += 1
                if len(candidate_files) < 200:
                    candidate_files.append(relative)
                if len(symbols) > max_symbols:
                    raise RoutingLimitExceeded("indexing", f"Repository produced more than {max_symbols} source symbols.")
                continue
        try:
            source = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            continue
        cache_misses += 1
        if len(candidate_files) < 200:
            candidate_files.append(relative)
        files_indexed += 1
        bytes_read += len(source.encode("utf-8"))
        extracted = _python_symbols(root, path, source) if language == "python" else _generic_symbols(root, path, source, language)
        symbols.extend(extracted)
        if use_cache:
            next_cached_files[relative] = {
                "fingerprint": fingerprint,
                "sha256": hashlib.sha256(source.encode("utf-8")).hexdigest(),
                "symbols": [_symbol_to_dict(symbol) for symbol in extracted],
            }
        if len(symbols) > max_symbols:
            raise RoutingLimitExceeded(
                "indexing", f"Repository produced more than {max_symbols} source symbols."
            )
        if progress and (files_indexed == 1 or files_indexed % 250 == 0):
            progress({
                "files_seen": files_seen,
                "files_indexed": files_indexed,
                "symbols_indexed": len(symbols),
                "bytes_read": bytes_read,
                "current_file": relative,
            })
    removed_files = len(set(cached_files) - set(next_cached_files)) if use_cache else 0
    cache_written = (
        _write_cache(cache_path, {"version": _CACHE_VERSION, "files": next_cached_files})
        if use_cache and (cache_misses or removed_files) else False
    )
    cache_status = (
        "disabled" if not use_cache else
        "warm" if cache_hits and not cache_misses and not removed_files else
        "incremental" if cache_hits else "cold"
    )
    stats = {
        "files_seen": files_seen,
        "files_indexed": files_indexed,
        "symbols_indexed": len(symbols),
        "bytes_read": bytes_read,
        "candidate_files": candidate_files,
        "candidate_files_truncated": max(0, files_indexed - len(candidate_files)),
        "index_mode": "partial" if include_paths is not None else "full",
        "index_cache_hit": cache_status == "warm",
        "cache": {
            "enabled": use_cache, "status": cache_status, "hit": cache_status == "warm",
            "files_reused": cache_hits, "files_reparsed": cache_misses,
            "files_removed": removed_files, "written": cache_written,
        },
    }
    return CodeIndex(repository, symbols, stats)
