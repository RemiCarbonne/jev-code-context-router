from __future__ import annotations

import ast
import hashlib
import json
import os
import re
import time
from dataclasses import dataclass, field, replace
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Any, Callable, Iterable

from .config import DIAGNOSTIC_ID_LIMIT
from .errors import RoutingLimitExceeded, RoutingTimeout
from .models import Repository, Symbol, SourceSpan, RegionProposal, EvidenceRegion
from .regions import canonicalize
from .security import PathPolicy
from .text import tokens

EXTENSION_LANGUAGE = {
    ".py": "python", ".pyi": "python", ".js": "javascript", ".jsx": "javascript",
    ".mjs": "javascript", ".cjs": "javascript", ".ts": "typescript", ".tsx": "typescript",
    ".go": "go", ".rs": "rust", ".java": "java", ".kt": "kotlin", ".kts": "kotlin",
    ".php": "php", ".rb": "ruby", ".cs": "csharp", ".c": "c", ".h": "c",
    ".cpp": "cpp", ".cc": "cpp", ".hpp": "cpp", ".swift": "swift", ".scala": "scala",
    ".astro": "astro", ".vue": "vue", ".svelte": "svelte", ".html": "html",
    ".htm": "html", ".tmpl": "text", ".jinja": "text", ".j2": "text",
}
_CACHE_VERSION = 6


def _symbol_to_dict(symbol: Symbol) -> dict[str, Any]:
    return {
        "id": symbol.id, "path": symbol.path, "name": symbol.name,
        "qualname": symbol.qualname, "kind": symbol.kind, "language": symbol.language,
        "start_line": symbol.start_line, "end_line": symbol.end_line,
        "source": symbol.source, "imports": symbol.imports,
        "precision": list(symbol.precision),
        "calls": sorted(symbol.calls), "references": sorted(symbol.references),
        "start_byte": symbol.start_byte, "end_byte": symbol.end_byte,
    }


def _symbol_from_dict(raw: dict[str, Any]) -> Symbol:
    return Symbol(
        id=raw["id"], path=raw["path"], name=raw["name"], qualname=raw["qualname"],
        kind=raw["kind"], language=raw["language"], start_line=int(raw["start_line"]),
        end_line=int(raw["end_line"]), source=raw["source"], imports=raw.get("imports", ""),
        calls=frozenset(raw.get("calls", ())), references=frozenset(raw.get("references", ())),
        start_byte=int(raw["start_byte"]), end_byte=int(raw["end_byte"]),
        precision=tuple(raw.get("precision", ())),
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
    regions: tuple[EvidenceRegion, ...] = ()

    def __post_init__(self) -> None:
        self.regions_by_id = {region.region_id: region for region in self.regions}
        self.alias_to_region = {symbol.id: symbol.region_id for symbol in self.symbols}
        self.by_id = {symbol.id: symbol for symbol in self.symbols}
        self.candidates: list[Symbol] = []
        seen: set[str] = set()
        for symbol in self.symbols:
            rid = symbol.region_id or symbol.id
            if rid not in seen:
                canonical = replace(symbol, id=rid)
                self.candidates.append(canonical)
                self.by_id[rid] = canonical
                seen.add(rid)
        self.by_name: dict[str, list[Symbol]] = {}
        for symbol in self.symbols:
            self.by_name.setdefault(symbol.name, []).append(self.by_id[symbol.region_id or symbol.id])


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
        return [replace(s, precision=("syntax-fallback", "approximate-structure"))
                for s in _generic_symbols(root, path, source, "text")]
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
        start = min([start] + [d.lineno for d in getattr(node, "decorator_list", ())])
        result.append(Symbol(
            id=f"{relative}::{qualname}", path=relative, name=name, qualname=qualname,
            kind=kind, language="python", start_line=start, end_line=end,
            source="".join(lines[start - 1 : end]), imports=import_source,
            start_byte=len("".join(lines[:start - 1]).encode()),
            end_byte=len("".join(lines[:end]).encode()),
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
    return result + _remainder_symbols(relative, source, "python", result, import_source)


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
        body = "".join(lines[index:end])
        body_tokens = tokens(body)
        kind = "class" if re.search(r"\b(class|interface|struct|enum|trait|type)\b", line) else "function"
        result.append(Symbol(
            id=f"{relative}::{name}@{index + 1}", path=relative, name=name, qualname=name,
            kind=kind, language=language, start_line=index + 1, end_line=end,
            source=body, imports=imports, calls=frozenset(), references=frozenset(body_tokens),
            start_byte=len("".join(lines[:index]).encode()), end_byte=len("".join(lines[:end]).encode()),
        ))
    return result + _remainder_symbols(relative, source, language, result, imports)


def _remainder_symbols(relative: str, source: str, language: str, declarations: list[Symbol], imports: str) -> list[Symbol]:
    lines = source.splitlines(keepends=True)
    offsets = [0]
    for line in lines:
        offsets.append(offsets[-1] + len(line.encode()))
    covered = {i for symbol in declarations for i in range(symbol.start_line - 1, symbol.end_line)}
    result = []
    start = 0
    while start < len(lines):
        if start in covered:
            start += 1
            continue
        end = start + 1
        while end < len(lines) and end not in covered and end - start < 160:
            end += 1
        body = ''.join(lines[start:end])
        if body.strip():
            result.append(Symbol(
                id=f'{relative}::module@{start + 1}', path=relative, name=Path(relative).stem,
                qualname=f'module_{start + 1}', kind='module', language=language,
                start_line=start + 1, end_line=end, source=body, imports=imports,
                references=frozenset(tokens(body)), start_byte=offsets[start], end_byte=offsets[end],
            ))
        start = end
    return result


def _astro_symbols(root: Path, path: Path, source: str) -> list[Symbol]:
    """Index bounded, non-overlapping Astro evidence regions.

    This is intentionally a small scanner, not a JavaScript parser: delimiters
    are balanced while strings/templates are skipped and every region retains
    exact source offsets. Specialized regions own their spans; generic regions
    are emitted only from the remainder.
    """
    relative = path.relative_to(root).as_posix()
    lines = source.splitlines(keepends=True)
    result: list[Symbol] = []

    def add(name: str, start: int, end: int, kind: str, body: str) -> None:
        if not body.strip():
            return
        result.append(Symbol(
            id=f"{relative}::{name}@{start}", path=relative, name=name, qualname=name,
            kind=kind, language="astro", start_line=source[:start].count('\n') + 1,
            end_line=source[:end - 1].count('\n') + 1,
            source=body, imports="", references=frozenset(tokens(body)),
            start_byte=len(source[:start].encode()), end_byte=len(source[:end].encode()),
        ))

    spans: list[tuple[int, int, str, str]] = []
    frontmatter = re.match(r"^---[ \t]*\r?\n(?P<body>.*?)(?:^|\n)---[ \t]*(?:\r?\n|$)", source, re.S | re.M)
    fm_start = fm_end = 0
    if frontmatter:
        fm_start, fm_end = frontmatter.start("body"), frontmatter.end("body")
        fm = frontmatter.group("body")
        # Embedded declarations are parents of specialized object/body regions,
        # not remainder shards. Reuse the declaration adapter and translate its
        # UTF-8 coordinates back into this snapshot's character coordinates.
        fm_bytes = fm.encode()
        for declaration in _generic_symbols(root, path, fm, "typescript"):
            if declaration.kind == "module":
                continue
            start = fm_start + len(fm_bytes[:declaration.start_byte].decode())
            end = fm_start + len(fm_bytes[:declaration.end_byte].decode())
            spans.append((start, end, declaration.name, declaration.kind))
        for m in re.finditer(r"(?m)^\s*(?:import|export\s+(?:type|interface))\b[^\n]*(?:\n|$)", fm):
            spans.append((fm_start + m.start(), fm_start + m.end(), "imports", "declaration"))
        # Usage, rather than the local variable name, identifies these regions.
        for marker, name in (("Astro\\.props", "props"), (r"(?:Astro\\.url|canonical|site|layout)", "layout")):
            for m in re.finditer(marker, fm, re.I):
                begin = fm.rfind(";", 0, m.start()) + 1
                begin = max(begin, fm.rfind("\n", 0, m.start()) + 1)
                end = fm.find(";", m.end())
                end = len(fm) if end < 0 else end + 1
                spans.append((fm_start + begin, fm_start + end, name, "declaration"))
        for m in re.finditer(r"(?:const|let|var)\s+[A-Za-z_$][\w$]*\s*=\s*([\[{])", fm):
            close = _balanced_fragment(fm, m.start(1))
            if close is not None:
                spans.append((fm_start + m.start(), fm_start + close + 1, "navigation", "declaration"))
    template_begin = frontmatter.end() if frontmatter else 0
    for m in re.finditer(r"(?is)<(script|style)\b[^>]*>(.*?)</\1\s*>", source):
        # The complete block is the semantic unit; never leave opening/closing
        # tag shards for the generic template region.
        spans.append((m.start(), m.end(), m.group(1).lower(), "module"))

    # Build complete Astro/HTML element units. Attribute evidence points at the
    # enclosing unit rather than carving it into ``<Layout`` and ``</Layout>``
    # leftovers. This also makes parent and child evidence non-competing.
    tag_re = re.compile(r"(?is)</?([A-Za-z][\w:.-]*)(?:\s[^<>]*?)?/?>")
    open_tags: list[tuple[str, int, int, bool]] = []
    tag_units: list[tuple[int, int, str, str]] = []
    for match in tag_re.finditer(source, template_begin):
        raw = match.group(0)
        name = match.group(1).casefold()
        if raw.startswith("</"):
            for index in range(len(open_tags) - 1, -1, -1):
                open_name, start, _, self_closing = open_tags[index]
                if open_name == name and not self_closing:
                    del open_tags[index:]
                    tag_units.append((start, match.end(), open_name, "template"))
                    break
        else:
            self_closing = raw.rstrip().endswith("/>")
            if self_closing:
                tag_units.append((match.start(), match.end(), name, "template"))
            else:
                open_tags.append((name, match.start(), match.end(), False))
    # Unclosed components still form a complete available tag unit.
    tag_units.extend((start, end, name, kind) for name, start, end, _ in open_tags
                     for kind in ("template",))

    # An attribute expression is one atomic proof region. Its source is the
    # whole enclosing element, including specialized attributes and children.
    attr_re = re.compile(r"\b(?P<name>href|canonical|layout)\s*=\s*", re.I)
    for m in attr_re.finditer(source[template_begin:]):
        begin = template_begin + m.start()
        containing = [unit for unit in tag_units if unit[0] <= begin < unit[1]]
        if containing:
            start, end, _, _ = min(containing, key=lambda unit: unit[1] - unit[0])
            spans.append((start, end, m.group("name").lower(), "template-region"))
        else:
            spans.append((begin, template_begin + m.end(), m.group("name").lower(), "template-region"))
    spans.extend(tag_units)
    for pos, char in enumerate(source[template_begin:], template_begin):
        if char == "{" and (close := _balanced_fragment(source, pos)) is not None:
            if not any(a <= pos and close + 1 <= b for a, b, *_ in spans):
                spans.append((pos, close + 1, "template-expression", "template-region"))
    # Keep every named atomic proof, but use the union of all specialized
    # intervals when producing generic remainder regions. No overlap may turn
    # into a partial tag.
    unique_spans = list(dict.fromkeys(spans))
    for start, end, name, kind in sorted(unique_spans, key=lambda x: (x[0], x[1], x[2])):
        add(name, start, end, kind, source[start:end])
    generic_excluded: list[tuple[int, int]] = []
    for start, end, *_ in sorted(unique_spans):
        if generic_excluded and start <= generic_excluded[-1][1]:
            generic_excluded[-1] = (generic_excluded[-1][0], max(generic_excluded[-1][1], end))
        else:
            generic_excluded.append((start, end))
    for begin, end, name, kind in ((fm_start, fm_end, "frontmatter", "module"), (template_begin, len(source), "template", "template")):
        cursor = begin
        for a, b in generic_excluded:
            if b <= begin or a >= end: continue
            if cursor < a:
                remainder = source[cursor:a]
                # No punctuation/tag shards: generic regions must carry useful
                # body text and can never compete with an atomic specialist.
                if re.search(r"[A-Za-z_$][\w$-]{2,}", remainder) and not re.fullmatch(r"[\s<>=/;:,-]*", remainder):
                    add(name, cursor, a, kind, remainder)
            cursor = max(cursor, b)
        if cursor < end:
            remainder = source[cursor:end]
            if re.search(r"[A-Za-z_$][\w$-]{2,}", remainder) and not re.fullmatch(r"[\s<>=/;:,-]*", remainder):
                add(name, cursor, end, kind, remainder)
    # Stable ordering and a final interval invariant protect cached indexes.
    result.sort(key=lambda item: (item.start_line, item.end_line, item.id))
    return result


def _balanced_fragment(text: str, opening: int) -> int | None:
    pairs = {"[": "]", "{": "}"}
    stack: list[str] = []
    quote = None
    escaped = False
    for pos in range(opening, len(text)):
        char = text[pos]
        if quote:
            if escaped: escaped = False
            elif char == "\\": escaped = True
            elif char == quote: quote = None
            continue
        if char in "'\"`": quote = char
        elif char in pairs: stack.append(pairs[char])
        elif stack and char == stack[-1]:
            stack.pop()
            if not stack: return pos
    return None


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
    admitted_globs: tuple[str, ...] = (),
) -> CodeIndex:
    policy = policy or PathPolicy()
    root = repository.root.resolve()
    symbols: list[Symbol] = []
    regions: list[EvidenceRegion] = []
    files_seen = 0
    files_indexed = 0
    bytes_read = 0
    candidate_files: list[str] = []
    use_cache = include_paths is None and cache_path is not None
    cache = _load_cache(cache_path) if use_cache else {"version": _CACHE_VERSION, "files": {}}
    if cache.get('admission', []) != sorted(admitted_globs):
        cache = {"version": _CACHE_VERSION, "files": {}}
    cached_files: dict[str, Any] = cache["files"]
    next_cached_files: dict[str, Any] = {}
    cache_hits = 0
    cache_misses = 0

    def register(relative: str, raw: bytes, extracted: list[Symbol]) -> None:
        proposals = [RegionProposal(SourceSpan(s.start_byte, s.end_byte), (s.name,), (s.kind,), (s.language,),
                                    ('builtin-v6',), tuple(s.precision) or ('line-exact' if s.language == 'python' else 'approximate-structure',))
                     for s in extracted]
        canonical = canonicalize(relative, raw, proposals)
        by_span = {r.span: r for r in canonical}
        regions.extend(canonical)
        for symbol in extracted:
            region = by_span[SourceSpan(symbol.start_byte, symbol.end_byte)]
            if raw[symbol.start_byte:symbol.end_byte].decode() != symbol.source:
                raise ValueError('adapter source disagrees with original byte interval')
            symbols.append(replace(symbol, region_id=region.region_id, file_sha256=region.file_sha256, snapshot=raw))

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
        if not language and (include_paths is not None or any(fnmatchcase(path.relative_to(root).as_posix(), g) for g in admitted_globs)):
            language = 'text'
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
                raw = cached['snapshot'].encode('utf-8')
                if hashlib.sha256(raw).hexdigest() != cached['sha256']:
                    raise ValueError('invalid cached snapshot')
                register(relative, raw, extracted)
            except (KeyError, TypeError, ValueError):
                cached = None
            else:
                cache_hits += 1
                next_cached_files[relative] = cached
                files_indexed += 1
                if len(candidate_files) < 200:
                    candidate_files.append(relative)
                if len(symbols) > max_symbols:
                    raise RoutingLimitExceeded("indexing", f"Repository produced more than {max_symbols} source symbols.")
                continue
        try:
            raw = path.read_bytes()
            source = raw.decode('utf-8')
            if '\x00' in source:
                continue
        except (OSError, UnicodeError):
            continue
        cache_misses += 1
        if len(candidate_files) < 200:
            candidate_files.append(relative)
        files_indexed += 1
        bytes_read += len(source.encode("utf-8"))
        extracted = (_python_symbols(root, path, source) if language == "python"
                     else _astro_symbols(root, path, source) if language == "astro"
                     else _generic_symbols(root, path, source, language))
        register(relative, raw, extracted)
        if use_cache:
            next_cached_files[relative] = {
                "fingerprint": fingerprint,
                "sha256": hashlib.sha256(source.encode("utf-8")).hexdigest(),
                "symbols": [_symbol_to_dict(symbol) for symbol in extracted],
                "snapshot": source,
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
        _write_cache(cache_path, {"version": _CACHE_VERSION, "admission": sorted(admitted_globs), "files": next_cached_files})
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
        "indexed_symbol_ids": [symbol.id for symbol in symbols[:DIAGNOSTIC_ID_LIMIT]],
        "indexed_symbol_ids_count": len(symbols),
        "indexed_symbol_ids_truncated": len(symbols) > DIAGNOSTIC_ID_LIMIT,
        "index_mode": "partial" if include_paths is not None else "full",
        "index_cache_hit": cache_status == "warm",
        "cache": {
            "enabled": use_cache, "status": cache_status, "hit": cache_status == "warm",
            "files_reused": cache_hits, "files_reparsed": cache_misses,
            "files_removed": removed_files, "written": cache_written,
        },
    }
    return CodeIndex(repository, symbols, stats, tuple(regions))
