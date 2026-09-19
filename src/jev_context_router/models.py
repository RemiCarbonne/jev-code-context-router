from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class Repository:
    root: Path
    name: str
    aliases: tuple[str, ...] = ()
    description: str = ""
    languages: tuple[str, ...] = ()


@dataclass(frozen=True)
class Symbol:
    id: str
    path: str
    name: str
    qualname: str
    kind: str
    language: str
    start_line: int
    end_line: int
    source: str
    imports: str = ""
    calls: frozenset[str] = field(default_factory=frozenset)
    references: frozenset[str] = field(default_factory=frozenset)


@dataclass(frozen=True)
class Selection:
    ids: tuple[str, ...]
    input_tokens: int = 0
    output_tokens: int = 0
    seconds: float = 0.0
    reason: str = ""
    candidates_sent: int = 0
    prompt_bytes: int = 0
    estimated_prompt_tokens: int = 0


@dataclass(frozen=True)
class RouteResult:
    status: str
    query: str
    repository: Repository | None = None
    context: str = ""
    selected_symbols: tuple[str, ...] = ()
    included_symbols: tuple[str, ...] = ()
    metrics: dict[str, Any] = field(default_factory=dict)
    message: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "query": self.query,
            "repository": str(self.repository.root) if self.repository else None,
            "repository_name": self.repository.name if self.repository else None,
            "context": self.context,
            "selected_symbols": list(self.selected_symbols),
            "included_symbols": list(self.included_symbols),
            "metrics": self.metrics,
            "message": self.message,
        }
