from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    workspace_roots: tuple[Path, ...] = ()
    repository_aliases: dict[str, Path] = field(default_factory=dict)
    model: str = "jev-latest"
    endpoint: str = "https://api.typesafe.ai/v1/systemone"
    timeout_seconds: float = 15.0
    route_timeout_seconds: float = 30.0
    discovery_timeout_seconds: float = 5.0
    index_timeout_seconds: float = 15.0
    external_timeout_seconds: float = 18.0
    shortlist_size: int = 20
    max_selected: int = 4
    max_expanded_symbols: int = 20
    max_context_chars: int = 12_000
    candidate_chars: int = 1_500
    local_fallback_selected: int = 2
    repository_confidence: float = 0.58
    symbol_fit_threshold: float = 0.52
    symbol_current_threshold: float = 0.45
    max_file_bytes: int = 300_000
    max_source_files: int = 25_000
    max_symbols: int = 75_000
    lexical_enabled: bool = True
    lexical_timeout_seconds: float = 0.5
    lexical_max_files: int = 40
    lexical_max_terms: int = 12
    lexical_min_distinct_terms: int = 2
    lexical_min_margin: int = 1
    metrics_path: Path | None = None

    @classmethod
    def load(cls, path: Path | None = None, cwd: Path | None = None) -> "Settings":
        cwd = (cwd or Path.cwd()).resolve()
        config_path = path
        if config_path is None:
            env_path = os.environ.get("JEV_CONTEXT_CONFIG", "").strip()
            candidates = [Path(env_path)] if env_path else [cwd / "jev-context.toml", Path.home() / ".config" / "jev-context-router" / "config.toml"]
            config_path = next((item for item in candidates if item.is_file()), None)
        raw: dict = {}
        base = cwd
        if config_path and config_path.is_file():
            raw = tomllib.loads(config_path.read_text(encoding="utf-8"))
            base = config_path.parent.resolve()

        def resolve(value: str) -> Path:
            path_value = Path(os.path.expandvars(value)).expanduser()
            return (base / path_value).resolve() if not path_value.is_absolute() else path_value.resolve()

        env_roots = tuple(item for item in os.environ.get("JEV_CONTEXT_WORKSPACE_ROOTS", "").split(os.pathsep) if item)
        configured_roots = raw.get("workspace_roots", []) or env_roots
        roots = tuple(resolve(str(item)) for item in configured_roots)
        if not roots:
            roots = (cwd,)
        aliases = {str(name).lower(): resolve(str(value)) for name, value in raw.get("repository_aliases", {}).items()}
        key_map = {
            name: raw[name]
            for name in (
                "model", "endpoint", "timeout_seconds", "route_timeout_seconds", "discovery_timeout_seconds",
                "index_timeout_seconds", "external_timeout_seconds", "shortlist_size", "max_selected",
                "max_expanded_symbols", "max_context_chars", "candidate_chars",
                "local_fallback_selected", "repository_confidence", "symbol_fit_threshold",
                "symbol_current_threshold", "max_file_bytes", "max_source_files", "max_symbols",
                "lexical_enabled", "lexical_timeout_seconds", "lexical_max_files",
                "lexical_max_terms", "lexical_min_distinct_terms", "lexical_min_margin",
            )
            if name in raw
        }
        metrics = raw.get("metrics_path") or os.environ.get("JEV_CONTEXT_METRICS")
        return cls(workspace_roots=roots, repository_aliases=aliases, metrics_path=resolve(metrics) if metrics else None, **key_map)

    @property
    def api_key(self) -> str:
        return os.environ.get("TYPESAFE_API_KEY", "").strip()
