from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request
from typing import Protocol

from .config import Settings
from .models import Repository, Selection, Symbol

_SECRET_ASSIGNMENT = re.compile(r"(?i)\b(api[_-]?key|token|password|secret|credential)\s*[:=]\s*['\"]?[^\s,'\"]+")
_EMAIL = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.I)


def sanitize_candidate(text: str, limit: int) -> str:
    text = _SECRET_ASSIGNMENT.sub(lambda match: f"{match.group(1)}=[REDACTED]", text)
    text = _EMAIL.sub("[EMAIL]", text)
    return text[:limit]


class Selector(Protocol):
    def choose_repository(self, query: str, repositories: list[Repository], threshold: float) -> tuple[Repository | None, dict]: ...
    def select_symbols(self, query: str, repository: Repository, candidates: list[Symbol], settings: Settings) -> Selection: ...


class LocalSelector:
    def choose_repository(self, query: str, repositories: list[Repository], threshold: float) -> tuple[Repository | None, dict]:
        return None, {"provider": "local", "reason": "no semantic repository selection"}

    def select_symbols(self, query: str, repository: Repository, candidates: list[Symbol], settings: Settings) -> Selection:
        ids = tuple(symbol.id for symbol in candidates[: settings.local_fallback_selected])
        return Selection(ids, reason="local-shortlist")


class JevSelector:
    def __init__(self, api_key: str, settings: Settings):
        self.api_key = api_key.strip()
        self.settings = settings

    def _evaluate(self, state: dict, questions: dict) -> tuple[dict, dict, float]:
        payload = json.dumps({"state": state, "model": self.settings.model, "questions": questions}, ensure_ascii=False).encode()
        request = urllib.request.Request(
            self.settings.endpoint, data=payload, method="POST",
            headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json", "User-Agent": "jev-context-router/0.1"},
        )
        started = time.perf_counter()
        try:
            with urllib.request.urlopen(request, timeout=self.settings.timeout_seconds) as response:
                result = json.loads(response.read().decode())
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"TypeSafe request failed: {type(exc).__name__}") from exc
        answers = result.get("answers")
        if not isinstance(answers, dict):
            raise RuntimeError("TypeSafe response has no answers map")
        return answers, result.get("usage") or {}, time.perf_counter() - started

    @staticmethod
    def _noul(answers: dict, key: str) -> float:
        try:
            return float(answers.get(key, {}).get("noul", 0.0))
        except (TypeError, ValueError, AttributeError):
            return 0.0

    def choose_repository(self, query: str, repositories: list[Repository], threshold: float) -> tuple[Repository | None, dict]:
        if not repositories:
            return None, {"provider": "jev", "reason": "empty roster"}
        state = {
            "request": query,
            "repositories": {
                str(index): {"name": repo.name, "aliases": repo.aliases, "description": repo.description, "languages": repo.languages}
                for index, repo in enumerate(repositories)
            },
        }
        questions = {
            "unresolved": {"type": "noul", "instructions": "Is there insufficient evidence to identify exactly one repository for this coding request?"}
        }
        for index, repo in enumerate(repositories):
            questions[f"fit_{index}"] = {"type": "noul", "instructions": f"Is repository `{repo.name}` the specific project targeted by this request?"}
        answers, usage, seconds = self._evaluate(state, questions)
        scored = sorted(((self._noul(answers, f"fit_{index}"), index) for index in range(len(repositories))), reverse=True)
        unresolved = self._noul(answers, "unresolved")
        best, index = scored[0]
        second = scored[1][0] if len(scored) > 1 else 0.0
        selected = repositories[index] if best >= threshold and unresolved < 0.62 and best - second >= 0.06 else None
        return selected, {
            "provider": "jev", "input_tokens": int(usage.get("input_tokens") or 0),
            "output_tokens": int(usage.get("output_tokens") or 0), "seconds": seconds,
            "best": best, "second": second, "unresolved": unresolved,
        }

    def select_symbols(self, query: str, repository: Repository, candidates: list[Symbol], settings: Settings) -> Selection:
        if not candidates:
            return Selection((), reason="no-candidates")
        state = {
            "request": query,
            "repository": repository.name,
            "candidate_symbols": {
                str(index): {
                    "id": symbol.id, "kind": symbol.kind, "language": symbol.language,
                    "source": sanitize_candidate(symbol.source, settings.candidate_chars),
                }
                for index, symbol in enumerate(candidates)
            },
        }
        questions: dict[str, dict] = {}
        for index, symbol in enumerate(candidates):
            questions[f"fit_{index}"] = {"type": "noul", "instructions": f"Is symbol `{symbol.id}` directly useful for this coding request?"}
            questions[f"current_{index}"] = {"type": "noul", "instructions": f"Is symbol `{symbol.id}` safe and current for this repository task?"}
        answers, usage, seconds = self._evaluate(state, questions)
        scored = []
        for index, symbol in enumerate(candidates):
            fit = self._noul(answers, f"fit_{index}")
            current = self._noul(answers, f"current_{index}")
            scored.append((fit, current, symbol.id))
        selected = [symbol_id for fit, current, symbol_id in sorted(scored, reverse=True) if fit >= settings.symbol_fit_threshold and current >= settings.symbol_current_threshold]
        selected = selected[: settings.max_selected]
        reason = "jev"
        if not selected:
            selected = [symbol.id for symbol in candidates[: settings.local_fallback_selected]]
            reason = "jev-local-fallback"
        return Selection(
            tuple(selected), int(usage.get("input_tokens") or 0), int(usage.get("output_tokens") or 0), seconds, reason
        )
