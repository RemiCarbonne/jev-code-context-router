from __future__ import annotations

import re
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from .config import Settings
from .index import EXTENSION_LANGUAGE
from .models import Repository
from .security import PathPolicy

_PATH_RE = re.compile(
    r"(?<![A-Za-z0-9_])(?:[A-Za-z0-9_.-]+[\\/])+[A-Za-z0-9_.-]+"
    r"\.(?:pyi?|jsx?|mjs|cjs|tsx?|go|rs|java|kt|kts|php|rb|cs|c|h|cpp|cc|hpp|swift|scala)\b",
    re.I,
)
_WORD_RE = re.compile(r"\b[A-Za-z_$][A-Za-z0-9_$-]{3,}\b")
_STOPWORDS = frozenset({
    "about", "after", "again", "against", "avec", "avant", "behavior", "behaviour",
    "before", "change", "changing", "code", "correctif", "dans", "describe", "edit",
    "editing", "every", "exact", "failing", "files", "fonction", "function", "identify",
    "implement", "improve", "issue", "modifier", "preserve", "preserving", "problem",
    "refactor", "request", "sans", "should", "source", "symbols", "tests", "their",
    "these", "those", "through", "using", "without", "writes", "writing", "while",
})


@dataclass(frozen=True)
class LexicalResult:
    status: str
    paths: tuple[Path, ...] = ()
    high_confidence: bool = False
    reason: str = ""
    seconds: float = 0.0
    terms_count: int = 0
    matched_files: int = 0
    top_score: int = 0
    second_score: int = 0


def extract_lexical_terms(query: str, limit: int = 12) -> tuple[list[str], list[str]]:
    """Extract literal source paths and discriminating code/domain identifiers."""
    explicit: list[str] = []
    for match in _PATH_RE.findall(query):
        normalized = match.replace("\\", "/")
        while normalized.startswith("./"):
            normalized = normalized[2:]
        if normalized not in explicit:
            explicit.append(normalized)

    terms: list[str] = []
    for word in _WORD_RE.findall(query):
        lowered = word.lower()
        distinctive_shape = (
            "_" in word or "$" in word or word.isupper()
            or any(character.isupper() for character in word[1:])
            or len(word) >= 8
        )
        if lowered in _STOPWORDS or not distinctive_shape:
            continue
        if word not in terms and word not in explicit:
            terms.append(word)
        if len(terms) >= limit:
            break
    return terms, explicit[:limit]


def _allowed_explicit_paths(repository: Repository, values: list[str], policy: PathPolicy) -> list[Path]:
    result: list[Path] = []
    root = repository.root.resolve()
    for value in values:
        candidate = root / value
        if candidate.suffix.lower() not in EXTENSION_LANGUAGE or not policy.allows(root, candidate):
            continue
        if candidate not in result:
            result.append(candidate)
    return result


def _rg_command(executable: str, terms: list[str], policy: PathPolicy) -> list[str]:
    command = [
        executable, "-l", "-i", "-F", "--no-messages", "--max-count", "1",
        "--max-filesize", str(policy.max_file_bytes),
    ]
    for term in terms:
        command.extend(("-e", term))
    for extension in sorted(EXTENSION_LANGUAGE):
        command.extend(("-g", f"*{extension}"))
    for part in sorted(policy.excluded_parts):
        command.extend(("-g", f"!{part}/**"))
    command.append(".")
    return command


def retrieve_lexical_candidates(
    repository: Repository,
    query: str,
    settings: Settings,
    policy: PathPolicy,
) -> LexicalResult:
    started = time.perf_counter()
    if not settings.lexical_enabled:
        return LexicalResult("disabled", reason="lexical-disabled")

    terms, explicit_values = extract_lexical_terms(query, settings.lexical_max_terms)
    explicit_paths = _allowed_explicit_paths(repository, explicit_values, policy)
    if explicit_paths:
        return LexicalResult(
            "matched", tuple(explicit_paths), True, "explicit-source-path",
            time.perf_counter() - started, len(terms), len(explicit_paths),
            max(1, len(terms)), 0,
        )
    if not terms:
        return LexicalResult(
            "no-terms", reason="no-distinctive-terms", seconds=time.perf_counter() - started
        )

    executable = shutil.which("rg")
    if not executable:
        return LexicalResult(
            "unavailable", reason="ripgrep-unavailable", seconds=time.perf_counter() - started,
            terms_count=len(terms),
        )
    try:
        completed = subprocess.run(
            _rg_command(executable, terms, policy),
            cwd=repository.root,
            capture_output=True,
            text=True,
            timeout=settings.lexical_timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return LexicalResult(
            "timeout", reason="ripgrep-timeout", seconds=time.perf_counter() - started,
            terms_count=len(terms),
        )
    except OSError:
        return LexicalResult(
            "error", reason="ripgrep-execution-error", seconds=time.perf_counter() - started,
            terms_count=len(terms),
        )

    if completed.returncode not in {0, 1}:
        return LexicalResult(
            "error", reason=f"ripgrep-exit-{completed.returncode}",
            seconds=time.perf_counter() - started, terms_count=len(terms),
        )

    root = repository.root.resolve()
    paths: list[Path] = []
    for line in completed.stdout.splitlines():
        raw = line.strip()
        if not raw:
            continue
        candidate = (root / raw).resolve()
        if candidate.suffix.lower() not in EXTENSION_LANGUAGE or not policy.allows(root, candidate):
            continue
        if candidate not in paths:
            paths.append(candidate)
    if not paths:
        return LexicalResult(
            "no-match", reason="no-lexical-match", seconds=time.perf_counter() - started,
            terms_count=len(terms),
        )
    if len(paths) > settings.lexical_max_files:
        return LexicalResult(
            "too-broad", reason="too-many-matched-files", seconds=time.perf_counter() - started,
            terms_count=len(terms), matched_files=len(paths),
        )

    scored: list[tuple[int, str, Path]] = []
    for path in paths:
        try:
            source = path.read_text(encoding="utf-8", errors="replace").casefold()
        except OSError:
            continue
        relative = path.relative_to(root).as_posix()
        haystack = f"{relative.casefold()}\n{source}"
        score = sum(1 for term in terms if term.casefold() in haystack)
        scored.append((score, relative, path))
    scored.sort(key=lambda item: (-item[0], item[1]))
    if not scored:
        return LexicalResult(
            "no-match", reason="no-readable-match", seconds=time.perf_counter() - started,
            terms_count=len(terms),
        )
    top_score = scored[0][0]
    second_score = scored[1][0] if len(scored) > 1 else 0
    high_confidence = (
        top_score >= settings.lexical_min_distinct_terms
        and top_score - second_score >= settings.lexical_min_margin
    )
    return LexicalResult(
        "matched", tuple(item[2] for item in scored), high_confidence,
        "distinctive-term-concentration" if high_confidence else "insufficient-confidence",
        time.perf_counter() - started, len(terms), len(scored), top_score, second_score,
    )
