from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

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


@dataclass
class CodeIndex:
    repository: Repository
    symbols: list[Symbol]

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


def index_repository(repository: Repository, policy: PathPolicy | None = None) -> CodeIndex:
    policy = policy or PathPolicy()
    root = repository.root.resolve()
    symbols: list[Symbol] = []
    for path in sorted(root.rglob("*")):
        language = EXTENSION_LANGUAGE.get(path.suffix.lower())
        if not language or not policy.allows(root, path):
            continue
        try:
            source = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            continue
        symbols.extend(_python_symbols(root, path, source) if language == "python" else _generic_symbols(root, path, source, language))
    return CodeIndex(repository, symbols)
