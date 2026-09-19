from __future__ import annotations

import json
import math
import re
import time
import urllib.error
import urllib.request
from dataclasses import replace
from typing import Any, Protocol

from .config import DIAGNOSTIC_ID_LIMIT, Settings
from .errors import ExternalProviderError, ExternalResponseError
from .models import CandidateEvidence, CoverageRequirement, Repository, Selection, Symbol

_SECRET_ASSIGNMENT = re.compile(r"(?i)\b(api[_-]?key|token|password|secret|credential)\s*[:=]\s*['\"]?[^\s,'\"]+")
_EMAIL = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.I)
_USAGE_FIELDS = ("input_tokens", "output_tokens")
# Provider accounting is bounded by the request/route budgets.  A value above
# this is not a credible token count and must not reach metrics arithmetic.
_MAX_PROVIDER_TOKENS = 10_000_000


def _diagnostic_ids(values: list[str] | tuple[str, ...]) -> tuple[str, ...]:
    return tuple(values[:DIAGNOSTIC_ID_LIMIT])


def _diagnostic_metadata(collections: dict[str, list[str] | tuple[str, ...]]) -> tuple[dict[str, int], dict[str, bool]]:
    return (
        {name: len(values) for name, values in collections.items()},
        {name: len(values) > DIAGNOSTIC_ID_LIMIT for name, values in collections.items()},
    )


def sanitize_candidate(text: str, limit: int) -> str:
    text = _SECRET_ASSIGNMENT.sub(lambda match: f"{match.group(1)}=[REDACTED]", text)
    text = _EMAIL.sub("[EMAIL]", text)
    return text[:limit]


def normalize_alternatives(alternatives: dict[str, list[CandidateEvidence]]) -> dict[str, CandidateEvidence]:
    unique: dict[str, CandidateEvidence] = {}
    for rid, entries in alternatives.items():
        for item in entries:
            previous = unique.get(item.candidate_id)
            if previous and (previous.path, previous.region, previous.excerpt) != (item.path, item.region, item.excerpt):
                raise ValueError('contradictory candidate identity')
            bindings = set(item.requirement_ids) | {rid}
            if previous:
                bindings.update(previous.requirement_ids)
            unique[item.candidate_id] = replace(item, requirement_ids=tuple(sorted(bindings)))
    return unique


def payload_limit(settings: Settings) -> int:
    return settings.selector_max_input_tokens * 4 if settings.selector_max_input_bytes is None else settings.selector_max_input_bytes


def serialize_request(state: dict, questions: dict, settings: Settings) -> bytes:
    payload = {'state': state, 'model': settings.model, 'questions': questions}
    if 'alternatives' in state:
        payload['candidates'] = list(state['alternatives'].values())
        payload['state'] = dict(state, alternatives={key: value['id'] for key, value in state['alternatives'].items()})
    return json.dumps(payload, ensure_ascii=False, separators=(',', ':')).encode()


class Selector(Protocol):
    def choose_repository(self, query: str, repositories: list[Repository], threshold: float) -> tuple[Repository | None, dict]: ...
    def select_symbols(self, query: str, repository: Repository, candidates: list[Symbol], settings: Settings) -> Selection: ...


class LocalSelector:
    def choose_repository(self, query: str, repositories: list[Repository], threshold: float) -> tuple[Repository | None, dict]:
        return None, {"provider": "local", "reason": "no semantic repository selection"}

    def select_symbols(self, query: str, repository: Repository, candidates: list[Symbol], settings: Settings) -> Selection:
        ids = tuple(symbol.id for symbol in candidates[: settings.local_fallback_selected])
        shortlist = tuple(symbol.id for symbol in candidates)
        counts, truncated = _diagnostic_metadata({"candidate_ids": shortlist})
        return Selection(
            ids=ids, reason="local-shortlist", transport_status="not-attempted",
            response_validity="not-applicable", external_selection_outcome="not-attempted",
            fallback_used=True, fallback_reason="local-selector",
            shortlist_ids=_diagnostic_ids(shortlist),
            external_selected_ids=(),
            local_fallback_used=True, local_fallback_reason="local-selector",
            diagnostic_counts=counts, diagnostic_truncated=truncated,
        )


class JevSelector:
    def __init__(self, api_key: str, settings: Settings):
        self.api_key = api_key.strip()
        self.settings = settings
        self.last_payload_bytes = 0
        self.last_response_validity = "valid"
        self.last_invalid_answer_keys: tuple[str, ...] = ()

    def _evaluate(self, state: dict, questions: dict) -> tuple[dict, dict, float]:
        self.last_response_validity = "not-applicable"
        self.last_invalid_answer_keys = ()
        payload = serialize_request(state, questions, self.settings)
        self.last_payload_bytes = len(payload)
        if len(payload) > payload_limit(self.settings):
            raise ValueError('selector-budget-local-fallback')
        request = urllib.request.Request(
            self.settings.endpoint, data=payload, method="POST",
            headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json", "User-Agent": "jev-context-router/0.1"},
        )
        started = time.perf_counter()
        try:
            with urllib.request.urlopen(request, timeout=self.settings.timeout_seconds) as response:
                result = json.loads(response.read().decode())
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as exc:
            raise ExternalProviderError("TypeSafe", exc) from exc
        except (json.JSONDecodeError, ValueError, UnicodeDecodeError) as exc:
            self.last_response_validity = "invalid"
            raise ExternalResponseError(type(exc).__name__) from exc
        if not isinstance(result, dict) or set(result) != {"answers", "usage"}:
            self.last_response_validity = "invalid"
            raise ExternalResponseError("InvalidResponse")
        answers = result.get("answers")
        usage = result.get("usage")
        if not isinstance(answers, dict) or not isinstance(usage, dict) or not self._valid_usage(usage):
            self.last_response_validity = "invalid"
            raise ExternalResponseError("InvalidResponse")
        self.last_response_validity, self.last_invalid_answer_keys = self._classify_answers(answers, questions)
        return answers, usage, time.perf_counter() - started

    @staticmethod
    def _valid_usage(usage: Any) -> bool:
        if not isinstance(usage, dict):
            return False
        return set(usage) == set(_USAGE_FIELDS) and all(
            type(usage.get(field)) is int
            and 0 <= usage[field] <= _MAX_PROVIDER_TOKENS
            for field in _USAGE_FIELDS
        )

    @staticmethod
    def _score(answer: Any) -> float | None:
        if not isinstance(answer, dict) or set(answer) != {"noul"}:
            return None
        value = answer.get("noul")
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        try:
            value = float(value)
        except (OverflowError, TypeError, ValueError, AttributeError):
            return None
        return value if math.isfinite(value) and 0 <= value <= 1 else None

    @classmethod
    def _valid_score(cls, answers: dict, key: str) -> float | None:
        return cls._score(answers.get(key))

    @classmethod
    def _classify_answers(cls, answers: dict, questions: dict) -> tuple[str, tuple[str, ...]]:
        """Classify one provider answer shape consistently across selection paths."""
        expected_keys = set(questions)
        invalid = tuple(key for key, answer in answers.items() if cls._score(answer) is None)
        missing = expected_keys - set(answers)
        unexpected = set(answers) - expected_keys
        invalid_expected = sum(key in invalid for key in expected_keys)
        validity = (
            "invalid" if unexpected or invalid_expected
            else "partial" if missing
            else "valid"
        )
        return validity, invalid

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
        scored = [(score, index) for index in range(len(repositories))
                  if (score := self._valid_score(answers, f"fit_{index}")) is not None]
        scored.sort(reverse=True)
        unresolved = self._valid_score(answers, "unresolved")
        validity, _ = self._classify_answers(answers, questions)
        complete_and_valid = validity == "valid" and len(scored) == len(repositories) and unresolved is not None
        best, index = scored[0] if scored else (None, 0)
        second = scored[1][0] if len(scored) > 1 else 0.0
        selected = repositories[index] if complete_and_valid and best is not None and unresolved is not None and best >= threshold and unresolved < 0.62 and best - second >= 0.06 else None
        return selected, {
            "provider": "jev", "input_tokens": int(usage.get("input_tokens") or 0),
            "output_tokens": int(usage.get("output_tokens") or 0), "seconds": seconds,
            "best": best if scored else None, "second": second, "unresolved": unresolved,
            "transport_status": "ok", "response_validity": validity,
            "external_selection_outcome": "selected" if selected else "no-accepted",
            "semantic_fallback_used": selected is None,
            "semantic_fallback_reason": "repository-response-invalid" if validity != "valid" else "no-accepted-repository",
            "local_fallback_used": False, "local_fallback_reason": "",
        }

    def rerank_alternatives(
        self, query: str, repository: Repository, requirements: tuple[CoverageRequirement, ...],
        alternatives: dict[str, list[CandidateEvidence]], settings: Settings,
    ) -> Selection:
        groups = {rid: items for rid, items in alternatives.items() if len(items) > 1}
        unique = normalize_alternatives(alternatives)
        if not groups:
            return Selection((), reason="jev-not-useful-choice")
        # Allocate one slot per requirement first, then round-robin remaining
        # alternatives. A dominant group may not starve a rare obligation.
        entries: list[CandidateEvidence] = []
        seen: set[str] = set()
        queues = {rid: list(groups[rid]) for rid in sorted(groups)}
        while len(entries) < settings.selector_max_candidates:
            progressed = False
            for rid in sorted(queues):
                if queues[rid] and len(entries) < settings.selector_max_candidates:
                    entry = queues[rid].pop(0)
                    if entry.candidate_id not in seen:
                        entries.append(unique[entry.candidate_id])
                        seen.add(entry.candidate_id)
                    progressed = True
            if not progressed:
                break
        max_payload_bytes = min(payload_limit(settings), payload_limit(self.settings))
        excerpt_limit = settings.candidate_chars

        def build_payload(items: list[CandidateEvidence], excerpt_chars: int) -> tuple[dict, dict, int]:
            state = {
                "request": sanitize_candidate(query, 512), "repository": repository.name,
                "requirements": [{"id": r.id, "source": sanitize_candidate(r.source, 240), "mandatory": r.mandatory}
                                for r in requirements if r.id in groups],
                "alternatives": {str(i): {"id": e.candidate_id, "path": e.path, "region": e.region,
                    "requirement_ids": e.requirement_ids, "excerpt": sanitize_candidate(e.excerpt, excerpt_chars),
                    "reason": sanitize_candidate(e.reason, 160), "cost": e.cost} for i, e in enumerate(items)},
                "budget": {"max_candidates": settings.selector_max_candidates,
                           "max_input_tokens": settings.selector_max_input_tokens},
            }
            questions = {f"fit_{i}": {"type": "noul", "instructions": "Choose the useful admissible alternative."}
                         for i in range(len(items))}
            payload = serialize_request(state, questions, self.settings)
            return state, questions, len(payload)

        state, questions, payload_bytes = build_payload(entries, excerpt_limit)
        # Shrink evidence first, then remove candidates round-robin while
        # retaining one alternative for every represented requirement whenever
        # the serialized budget permits it.
        while payload_bytes > max_payload_bytes and excerpt_limit > 64:
            excerpt_limit = max(64, int(excerpt_limit * 0.7))
            state, questions, payload_bytes = build_payload(entries, excerpt_limit)
        while payload_bytes > max_payload_bytes and entries:
            protected = {next((e.candidate_id for e in entries if rid in e.requirement_ids), None) for rid in groups}
            remove_index = next((i for i, e in reversed(list(enumerate(entries))) if e.candidate_id not in protected), None)
            if remove_index is None:
                break
            entries.pop(remove_index)
            state, questions, payload_bytes = build_payload(entries, excerpt_limit)
        # Even the minimal external request must stay inside the hard budget.
        # A local result is safer than sending an oversized payload.
        represented = {rid for entry in entries for rid in entry.requirement_ids}
        if payload_bytes > max_payload_bytes or not set(groups) <= represented or settings.model != self.settings.model:
            ids = tuple(e.candidate_id for e in entries[:settings.local_fallback_selected])
            return Selection(ids=ids, reason="selector-budget-local-fallback", candidates_sent=0,
                prompt_bytes=payload_bytes, estimated_prompt_tokens=math.ceil(payload_bytes / 4),
                transport_status="not-attempted", response_validity="not-applicable",
                external_selection_outcome="budget-excluded", fallback_used=True,
                fallback_reason="reranker-budget", semantic_fallback_used=True,
                semantic_fallback_reason="reranker-budget", local_fallback_used=True,
                local_fallback_reason="semantic-fallback", shortlist_ids=tuple(e.candidate_id for e in entries),
                diagnostic_counts={"candidate_ids": len(entries)},
                ledger={'proposed': tuple(unique), 'sent': (), 'score_valid': (), 'score_missing': (), 'score_invalid': (), 'external_selected': ()},
                prepared_payload_bytes=payload_bytes, sent_payload_bytes=0,
            )
        self.last_response_validity = 'not-applicable'
        self.last_payload_bytes = payload_bytes
        answers, usage, seconds = self._evaluate(state, questions)
        validity, invalid_keys = self._classify_answers(answers, questions)
        self.last_response_validity = validity
        sent = tuple(e.candidate_id for e in entries)
        valid = tuple(e.candidate_id for i, e in enumerate(entries) if self._valid_score(answers, f'fit_{i}') is not None)
        missing = tuple(e.candidate_id for i, e in enumerate(entries) if f'fit_{i}' not in answers)
        invalid = tuple(e.candidate_id for i, e in enumerate(entries) if f'fit_{i}' in invalid_keys)
        ledger = {'proposed': tuple(unique), 'sent': sent, 'score_valid': valid, 'score_missing': missing,
                  'score_invalid': invalid, 'external_selected': ()}
        if self.last_response_validity != "valid":
            # Partial/invalid answers are never an external success.  Keep the
            # transport accounting (the call did happen), but explicitly fall
            # back to the local semantic shortlist.
            ids = tuple(e.candidate_id for e in entries[:settings.local_fallback_selected])
            return Selection(ids=ids, input_tokens=int(usage.get("input_tokens", 0)),
                output_tokens=int(usage.get("output_tokens", 0)), seconds=seconds,
                reason="jev-semantic-fallback", candidates_sent=len(entries),
                prompt_bytes=self.last_payload_bytes,
                estimated_prompt_tokens=math.ceil(self.last_payload_bytes / 4),
                transport_status="ok", response_validity=self.last_response_validity,
                external_selection_outcome="no-accepted", fallback_used=True,
                fallback_reason=f"external-response-{self.last_response_validity}",
                semantic_fallback_used=True,
                semantic_fallback_reason=f"external-response-{self.last_response_validity}",
                local_fallback_used=True, local_fallback_reason="semantic-fallback",
                shortlist_ids=_diagnostic_ids(sent), candidates_sent_ids=_diagnostic_ids(sent),
                scored_ids=_diagnostic_ids(valid), invalid_score_ids=_diagnostic_ids(invalid), ledger=ledger,
                prepared_payload_bytes=payload_bytes, sent_payload_bytes=payload_bytes)
        scores = [(self._valid_score(answers, f"fit_{i}"), entry) for i, entry in enumerate(entries)]
        chosen = []
        for rid in sorted(groups):
            options = [(score, entry.candidate_id) for score, entry in scores
                       if score is not None and score >= settings.symbol_fit_threshold
                       and rid in entry.requirement_ids]
            if options:
                # Reranking orders admissible alternatives; it must not erase
                # independently useful high-scoring files in a transversal
                # request merely because they share one requirement.
                chosen.extend(item_id for _, item_id in sorted(options, reverse=True)[:settings.max_selected])
        if not chosen:
            ids = tuple(e.candidate_id for e in entries[:settings.local_fallback_selected])
            return Selection(ids=ids, input_tokens=int(usage.get("input_tokens", 0)),
            output_tokens=int(usage.get("output_tokens", 0)), seconds=seconds,
            reason="jev-semantic-fallback", candidates_sent=len(entries),
            prompt_bytes=self.last_payload_bytes, estimated_prompt_tokens=math.ceil(self.last_payload_bytes / 4),
            transport_status="ok", response_validity=self.last_response_validity,
            external_selection_outcome="no-accepted", fallback_used=True,
            fallback_reason="no-external-candidate-above-threshold", semantic_fallback_used=True,
            semantic_fallback_reason="no-external-candidate-above-threshold", local_fallback_used=True,
            local_fallback_reason="semantic-fallback", candidates_sent_ids=tuple(e.candidate_id for e in entries),
            shortlist_ids=_diagnostic_ids(sent), external_selected_ids=(),
            scored_ids=_diagnostic_ids(valid), ledger=ledger,
            prepared_payload_bytes=payload_bytes, sent_payload_bytes=payload_bytes)
        return Selection(ids=tuple(dict.fromkeys(chosen)), input_tokens=int(usage.get("input_tokens", 0)),
            output_tokens=int(usage.get("output_tokens", 0)), seconds=seconds, reason="jev-rerank",
            candidates_sent=len(entries), prompt_bytes=self.last_payload_bytes,
            estimated_prompt_tokens=math.ceil(self.last_payload_bytes / 4), transport_status="ok",
            response_validity=self.last_response_validity, external_selection_outcome="reranked",
            external_selected_ids=_diagnostic_ids(tuple(dict.fromkeys(chosen))), shortlist_ids=_diagnostic_ids(sent),
            candidates_sent_ids=_diagnostic_ids(sent), scored_ids=_diagnostic_ids(valid),
            ledger=dict(ledger, external_selected=tuple(dict.fromkeys(chosen))),
            prepared_payload_bytes=payload_bytes, sent_payload_bytes=payload_bytes)

    def select_symbols(self, query: str, repository: Repository, candidates: list[Symbol], settings: Settings) -> Selection:
        if not candidates:
            return Selection(ids=(), reason="no-candidates")
        shortlist_full = tuple(symbol.id for symbol in candidates)
        shortlist_ids = _diagnostic_ids(shortlist_full)
        bounded = list(candidates[: settings.selector_max_candidates])
        excluded_budget = list(candidates[settings.selector_max_candidates:])
        request_text = sanitize_candidate(query, max(256, settings.selector_max_input_tokens * 2))

        def compact(symbol: Symbol) -> dict:
            first_lines = "\n".join(line.strip() for line in symbol.source.splitlines()[:3] if line.strip())
            return {
                "path": symbol.path, "symbol": symbol.qualname, "kind": symbol.kind,
                "lang": symbol.language, "signature": sanitize_candidate(first_lines, settings.candidate_chars),
            }

        def build(items: list[Symbol]) -> tuple[dict, dict]:
            state = {
                "request": request_text, "repository": repository.name,
                "candidate_symbols": {str(index): compact(symbol) for index, symbol in enumerate(items)},
            }
            questions = {
                f"fit_{index}": {"type": "noul", "instructions": "Directly useful?"}
                for index in range(len(items))
            }
            return state, questions

        max_payload_bytes = min(payload_limit(settings), payload_limit(self.settings))

        def payload_size(state: dict, questions: dict) -> int:
            return len(json.dumps(
                {"state": state, "model": settings.model, "questions": questions},
                ensure_ascii=False, separators=(",", ":"),
            ).encode())

        while len(bounded) > 1:
            state, questions = build(bounded)
            if payload_size(state, questions) <= max_payload_bytes:
                break
            excluded_budget.append(bounded.pop())
        state, questions = build(bounded)
        while payload_size(state, questions) > max_payload_bytes and len(request_text) > 64:
            request_text = request_text[: max(64, int(len(request_text) * 0.8))]
            state, questions = build(bounded)
        estimated_bytes = payload_size(state, questions)
        if estimated_bytes > max_payload_bytes:
            ids = tuple(symbol.id for symbol in bounded[: settings.local_fallback_selected])
            counts, truncated = _diagnostic_metadata({
                "candidate_ids": shortlist_full,
                "candidates_sent_ids": (),
                "excluded_budget_ids": tuple(symbol.id for symbol in excluded_budget),
            })
            return Selection(
                ids=ids, reason="selector-budget-local-fallback", candidates_sent=0,
                prompt_bytes=estimated_bytes, estimated_prompt_tokens=math.ceil(estimated_bytes / 4),
                transport_status="not-attempted", response_validity="not-applicable",
                external_selection_outcome="budget-excluded", fallback_used=True,
                fallback_reason="selector-budget", shortlist_ids=shortlist_ids,
                excluded_budget_ids=_diagnostic_ids(tuple(symbol.id for symbol in excluded_budget)),
                diagnostic_counts=counts, diagnostic_truncated=truncated,
            )
        answers, usage, seconds = self._evaluate(state, questions)
        scored = []
        interpreted_scores: dict[str, float] = {}
        invalid_ids = []
        for index, symbol in enumerate(bounded):
            score = self._valid_score(answers, f"fit_{index}")
            if score is None:
                invalid_ids.append(symbol.id)
                continue
            scored.append((score, symbol.id))
            interpreted_scores[symbol.id] = score
        response_validity, _ = self._classify_answers(answers, questions)
        external_selected = []
        if response_validity == "valid":
            external_selected = [
                symbol_id for fit, symbol_id in sorted(scored, reverse=True)
                if fit >= settings.symbol_fit_threshold
            ][: settings.max_selected]
        selected = list(external_selected)
        reason = "jev"
        fallback_used = False
        fallback_reason = ""
        outcome = "selected" if external_selected else "no-accepted"
        if not external_selected:
            selected = [symbol.id for symbol in bounded[: settings.local_fallback_selected]]
            reason = "jev-local-fallback"
            fallback_used = True
            fallback_reason = (
                f"external-response-{response_validity}"
                if response_validity != "valid"
                else "no-external-candidate-above-threshold"
            )
        prompt_bytes = self.last_payload_bytes or estimated_bytes
        candidates_sent_full = tuple(symbol.id for symbol in bounded)
        scored_full = tuple(symbol_id for _, symbol_id in scored)
        invalid_full = tuple(invalid_ids)
        external_selected_full = tuple(external_selected)
        excluded_budget_full = tuple(symbol.id for symbol in excluded_budget)
        excluded_score_full = tuple(symbol_id for fit, symbol_id in scored if fit < settings.symbol_fit_threshold)
        counts, truncated = _diagnostic_metadata({
            "candidate_ids": shortlist_full, "candidates_sent_ids": candidates_sent_full,
            "scored_ids": scored_full, "invalid_score_ids": invalid_full,
            "external_selected_ids": external_selected_full, "excluded_budget_ids": excluded_budget_full,
            "excluded_score_ids": excluded_score_full,
        })
        return Selection(
            ids=tuple(selected), input_tokens=int(usage.get("input_tokens") or 0),
            output_tokens=int(usage.get("output_tokens") or 0), seconds=seconds, reason=reason,
            candidates_sent=len(bounded), prompt_bytes=prompt_bytes,
            estimated_prompt_tokens=math.ceil(prompt_bytes / 4), transport_status="ok",
            response_validity=response_validity, external_selection_outcome=outcome,
            fallback_used=fallback_used, fallback_reason=fallback_reason,
            shortlist_ids=shortlist_ids, candidates_sent_ids=_diagnostic_ids(candidates_sent_full),
            scored_ids=_diagnostic_ids(scored_full), invalid_score_ids=_diagnostic_ids(invalid_full),
            external_selected_ids=_diagnostic_ids(external_selected_full),
            excluded_budget_ids=_diagnostic_ids(excluded_budget_full),
            excluded_score_ids=_diagnostic_ids(excluded_score_full),
            semantic_fallback_used=fallback_used,
            semantic_fallback_reason=fallback_reason if fallback_used else "",
            local_fallback_used=fallback_used,
            local_fallback_reason="semantic-fallback" if fallback_used else "",
            interpreted_scores={symbol_id: score for symbol_id, score in list(interpreted_scores.items())[:DIAGNOSTIC_ID_LIMIT]},
            diagnostic_counts=counts, diagnostic_truncated=truncated,
            ledger={'proposed': shortlist_full, 'sent': candidates_sent_full,
                    'score_valid': scored_full,
                    'score_missing': tuple(s.id for i, s in enumerate(bounded) if f'fit_{i}' not in answers),
                    'score_invalid': tuple(s.id for i, s in enumerate(bounded) if f'fit_{i}' in answers and s.id in invalid_full),
                    'external_selected': external_selected_full},
            prepared_payload_bytes=estimated_bytes, sent_payload_bytes=prompt_bytes,
        )
