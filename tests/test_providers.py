import json
import urllib.error
from email.message import Message
from pathlib import Path

import pytest
from jev_context_router.config import Settings
from jev_context_router.errors import ExternalResponseError
from jev_context_router.models import CandidateEvidence, CoverageRequirement, Repository, Symbol
from jev_context_router.providers import JevSelector
from jev_context_router.router import ContextRouter


class StubJev(JevSelector):
    def __init__(self, answers):
        super().__init__("test-key", Settings())
        self.answers = answers
        self.state = None

    def _evaluate(self, state, questions):
        self.state = state
        return self.answers, {"input_tokens": 10, "output_tokens": 2}, 0.01


def test_repository_selection_requires_confidence_and_margin(tmp_path):
    repositories = [Repository(tmp_path / "alpha", "alpha"), Repository(tmp_path / "beta", "beta")]
    selector = StubJev({"unresolved": {"noul": 0.1}, "fit_0": {"noul": 0.9}, "fit_1": {"noul": 0.2}})
    chosen, metrics = selector.choose_repository("fix alpha", repositories, 0.58)
    assert chosen is repositories[0]
    assert metrics["best"] == 0.9

    ambiguous = StubJev({"unresolved": {"noul": 0.1}, "fit_0": {"noul": 0.9}, "fit_1": {"noul": 0.87}})
    chosen, _ = ambiguous.choose_repository("fix it", repositories, 0.58)
    assert chosen is None


def test_repository_partial_response_never_selects_external_repository(tmp_path):
    repositories = [Repository(tmp_path / "alpha", "alpha"), Repository(tmp_path / "beta", "beta")]
    selector = StubJev({"unresolved": {"noul": 0.1}, "fit_0": {"noul": 0.9}})
    chosen, metrics = selector.choose_repository("fix alpha", repositories, 0.58)
    assert chosen is None
    assert metrics["response_validity"] == "partial"
    assert metrics["external_selection_outcome"] == "no-accepted"
    assert metrics["transport_status"] == "ok"


def test_repository_malformed_response_never_selects_external_repository(tmp_path):
    repositories = [Repository(tmp_path / "alpha", "alpha"), Repository(tmp_path / "beta", "beta")]
    selector = StubJev({"unresolved": {"noul": 0.1}, "fit_0": "bad", "fit_1": {"noul": 0.2}})
    chosen, metrics = selector.choose_repository("fix alpha", repositories, 0.58)
    assert chosen is None
    assert metrics["response_validity"] == "invalid"
    assert metrics["external_selection_outcome"] == "no-accepted"


def test_repository_extra_malformed_answer_invalidates_entire_response(tmp_path):
    repositories = [Repository(tmp_path / "alpha", "alpha"), Repository(tmp_path / "beta", "beta")]
    selector = StubJev({
        "unresolved": {"noul": 0.1}, "fit_0": {"noul": 0.9}, "fit_1": {"noul": 0.2},
        "unexpected": {"noul": "not-a-score"},
    })
    chosen, metrics = selector.choose_repository("fix alpha", repositories, 0.58)
    assert chosen is None
    assert metrics["response_validity"] == "invalid"
    assert metrics["external_selection_outcome"] == "no-accepted"


def test_symbol_selection_redacts_before_remote_evaluation(tmp_path):
    repository = Repository(tmp_path, "demo")
    symbols = [
        Symbol(
            id="service.py::run", path="service.py", name="run", qualname="run",
            kind="function", language="python", start_line=1, end_line=2,
            source="def run():\n    token = 'very-secret'\n", calls=frozenset({"run"}),
        ),
        Symbol(
            id="other.py::noop", path="other.py", name="noop", qualname="noop",
            kind="function", language="python", start_line=1, end_line=1,
            source="def noop(): pass",
        ),
    ]
    selector = StubJev({
        "fit_0": {"noul": 0.95},
        "fit_1": {"noul": 0.1},
    })
    selection = selector.select_symbols("fix run", repository, symbols, Settings())
    assert selection.ids == ("service.py::run",)
    sent = selector.state["candidate_symbols"]["0"]["signature"]
    assert "very-secret" not in sent
    assert "[REDACTED]" in sent


def test_empty_jev_symbol_selection_falls_back_locally(tmp_path):
    repository = Repository(tmp_path, "demo")
    symbols = [Symbol(
        id="a.py::a", path="a.py", name="a", qualname="a", kind="function",
        language="python", start_line=1, end_line=1, source="def a(): pass",
    )]
    selector = StubJev({"fit_0": {"noul": 0.1}})
    selection = selector.select_symbols("fix it", repository, symbols, Settings())
    assert selection.ids == ("a.py::a",)
    assert selection.reason == "jev-local-fallback"


def test_all_low_scores_are_an_explicit_semantic_fallback_with_candidate_ids(tmp_path):
    repository = Repository(tmp_path, "demo")
    symbols = [Symbol(
        id=f"a{index}.py::a{index}", path=f"a{index}.py", name=f"a{index}",
        qualname=f"a{index}", kind="function", language="python", start_line=1,
        end_line=1, source=f"def a{index}(): pass",
    ) for index in range(2)]
    selection = StubJev({"fit_0": {"noul": 0.1}, "fit_1": {"noul": 0.2}}).select_symbols(
        "fix it", repository, symbols, Settings()
    )
    assert selection.external_selection_outcome == "no-accepted"
    assert selection.fallback_used is True
    assert selection.fallback_reason == "no-external-candidate-above-threshold"
    assert selection.shortlist_ids == tuple(symbol.id for symbol in symbols)
    assert selection.external_selected_ids == ()


def test_partial_response_is_not_reported_as_valid_zero_scores(tmp_path):
    repository = Repository(tmp_path, "demo")
    symbols = [Symbol(id=f"a{index}", path=f"a{index}.py", name=f"a{index}", qualname=f"a{index}",
                      kind="function", language="python", start_line=1, end_line=1, source="pass")
               for index in range(2)]
    selection = StubJev({"fit_0": {"noul": 0.9}}).select_symbols("fix it", repository, symbols, Settings())
    assert selection.response_validity == "partial"
    assert "a1" in selection.invalid_score_ids
    assert selection.interpreted_scores == {"a0": 0.9}
    assert selection.external_selection_outcome == "no-accepted"
    assert selection.external_selected_ids == ()
    assert selection.fallback_used is True
    assert selection.fallback_reason == "external-response-partial"


def test_malformed_response_is_explicitly_invalid(tmp_path):
    repository = Repository(tmp_path, "demo")
    symbol = Symbol(id="a", path="a.py", name="a", qualname="a", kind="function",
                    language="python", start_line=1, end_line=1, source="pass")
    selector = StubJev({"fit_0": "not-a-score"})
    selection = selector.select_symbols("fix it", repository, [symbol], Settings())
    assert selection.response_validity == "invalid"
    assert selection.fallback_used is True
    assert selection.invalid_score_ids == ("a",)
    assert selection.external_selection_outcome == "no-accepted"
    assert selection.external_selected_ids == ()
    assert selection.fallback_reason == "external-response-invalid"


@pytest.mark.parametrize("score", [True, False])
def test_boolean_scores_are_invalid_not_numeric(tmp_path, score):
    repository = Repository(tmp_path, "demo")
    symbol = Symbol(id="a", path="a.py", name="a", qualname="a", kind="function",
                    language="python", start_line=1, end_line=1, source="pass")
    selection = StubJev({"fit_0": {"noul": score}}).select_symbols("fix it", repository, [symbol], Settings())
    assert selection.invalid_score_ids == ("a",)
    assert selection.interpreted_scores == {}
    assert selection.response_validity == "invalid"


def test_semantic_and_local_fallbacks_are_distinct(tmp_path):
    repository = Repository(tmp_path, "demo")
    symbol = Symbol(id="a", path="a.py", name="a", qualname="a", kind="function",
                    language="python", start_line=1, end_line=1, source="pass")
    selection = StubJev({"fit_0": {"noul": 0.1}}).select_symbols("fix it", repository, [symbol], Settings())
    assert selection.semantic_fallback_used is True
    assert selection.semantic_fallback_reason == "no-external-candidate-above-threshold"
    assert selection.local_fallback_used is True
    assert selection.local_fallback_reason == "semantic-fallback"


def test_symbol_selector_compacts_and_bounds_remote_payload(tmp_path):
    repository = Repository(tmp_path, "demo")
    symbols = [Symbol(
        id=f"src/file_{index}.ts::symbol_{index}", path=f"src/file_{index}.ts",
        name=f"symbol_{index}", qualname=f"symbol_{index}", kind="function",
        language="typescript", start_line=1, end_line=100,
        source="export function symbol() {\n" + ("const verbose = 1;\n" * 300) + "}",
    ) for index in range(30)]
    selector = StubJev({f"fit_{index}": {"noul": 0.9} for index in range(12)})
    settings = Settings(selector_max_candidates=12, selector_max_input_tokens=600, candidate_chars=120)
    selection = selector.select_symbols("Debug the TypeScript pipeline", repository, symbols, settings)
    serialized = json.dumps({"state": selector.state, "questions": {}}, ensure_ascii=False)
    assert selection.candidates_sent <= 12
    assert selection.estimated_prompt_tokens <= 600
    assert len(serialized.encode()) < 2_400
    assert all(len(item["signature"]) <= 120 for item in selector.state["candidate_symbols"].values())


def test_budget_exclusions_are_identified(tmp_path):
    repository = Repository(tmp_path, "demo")
    symbols = [Symbol(id=f"file{index}::symbol", path=f"file{index}.py", name="symbol",
                      qualname="symbol", kind="function", language="python", start_line=1,
                      end_line=1, source="x" * 400) for index in range(4)]
    selector = StubJev({"fit_0": {"noul": 0.9}})
    selection = selector.select_symbols(
        "Debug this very long request", repository, symbols,
        Settings(selector_max_candidates=4, selector_max_input_tokens=30, candidate_chars=120),
    )
    assert selection.excluded_budget_ids
    assert set(selection.excluded_budget_ids) <= {symbol.id for symbol in symbols}


def test_diagnostic_identifier_collections_have_an_independent_cap(tmp_path):
    repository = Repository(tmp_path, "demo")
    symbols = [Symbol(id=f"a{index}", path=f"a{index}.py", name=f"a{index}", qualname=f"a{index}",
                      kind="function", language="python", start_line=1, end_line=1, source="pass")
               for index in range(80)]
    answers = {f"fit_{index}": {"noul": 0.1} for index in range(80)}
    selection = StubJev(answers).select_symbols(
        "fix it", repository, symbols,
        Settings(selector_max_candidates=80, selector_max_input_tokens=100_000, max_selected=80),
    )
    assert len(selection.shortlist_ids) <= 32
    assert len(selection.candidates_sent_ids) <= 32
    assert len(selection.scored_ids) <= 32
    assert len(selection.excluded_score_ids) <= 32


@pytest.mark.parametrize(("size", "truncated"), [(32, False), (33, True)])
def test_diagnostic_counts_are_before_identifier_truncation(tmp_path, size, truncated):
    repository = Repository(tmp_path, "demo")
    symbols = [Symbol(id=f"a{index}", path=f"a{index}.py", name=f"a{index}", qualname=f"a{index}",
                      kind="function", language="python", start_line=1, end_line=1, source="pass")
               for index in range(size)]
    answers = {f"fit_{index}": {"noul": 0.1} for index in range(size)}
    selection = StubJev(answers).select_symbols("fix it", repository, symbols,
                                                Settings(selector_max_candidates=size,
                                                         selector_max_input_tokens=100_000))
    assert len(selection.shortlist_ids) == 32
    assert selection.diagnostic_counts["candidate_ids"] == size
    assert selection.diagnostic_truncated["candidate_ids"] is truncated


def test_huge_integer_score_is_invalid_not_a_transport_failure(tmp_path):
    repository = Repository(tmp_path, "demo")
    symbol = Symbol(id="a", path="a.py", name="a", qualname="a", kind="function",
                    language="python", start_line=1, end_line=1, source="pass")
    selection = StubJev({"fit_0": {"noul": 10 ** 10_000}}).select_symbols(
        "fix it", repository, [symbol], Settings()
    )
    assert selection.response_validity == "invalid"
    assert selection.invalid_score_ids == ("a",)
    assert selection.transport_status == "ok"
    assert selection.external_selection_outcome == "no-accepted"


def test_valid_unexpected_symbol_answer_key_is_invalid(tmp_path):
    repository = Repository(tmp_path, "demo")
    symbol = Symbol(id="a", path="a.py", name="a", qualname="a", kind="function",
                    language="python", start_line=1, end_line=1, source="pass")
    selection = StubJev({"fit_0": {"noul": 0.9}, "unexpected": {"noul": 0.8}}).select_symbols(
        "fix it", repository, [symbol], Settings()
    )
    assert selection.response_validity == "invalid"
    assert selection.external_selected_ids == ()


def test_evaluate_rejects_unexpected_valid_answer_key(monkeypatch):
    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return json.dumps({
                "answers": {"fit_0": {"noul": 0.9}, "fit_extra": {"noul": 0.8}},
                "usage": {"input_tokens": 1, "output_tokens": 1},
            }).encode()

    monkeypatch.setattr("urllib.request.urlopen", lambda *args, **kwargs: Response())
    selector = JevSelector("test-key", Settings())
    answers, _, _ = selector._evaluate({}, {"fit_0": {"type": "noul"}})
    assert answers["fit_extra"]["noul"] == 0.8
    assert selector.last_response_validity == "invalid"


@pytest.mark.parametrize("score", [-0.01, 1.01, 10 ** 10, float("nan"), float("inf"), True, "0.5"])
def test_scores_must_be_finite_numbers_in_unit_interval(score):
    assert JevSelector._score({"noul": score}) is None


@pytest.mark.parametrize("score", [0, 0.5, 1, 1.0])
def test_scores_in_unit_interval_are_accepted(score):
    assert JevSelector._score({"noul": score}) == float(score)


@pytest.mark.parametrize("usage", [None, [], "bad"])
def test_provider_rejects_non_mapping_usage(monkeypatch, usage):
    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return json.dumps({"answers": {"fit_0": {"noul": 0.9}}, "usage": usage}).encode()

    monkeypatch.setattr("urllib.request.urlopen", lambda *args, **kwargs: Response())
    selector = JevSelector("test-key", Settings())
    with pytest.raises(ExternalResponseError):
        selector._evaluate({}, {"fit_0": {"type": "noul"}})
    assert selector.last_response_validity == "invalid"


@pytest.mark.parametrize(
    ("usage", "score", "expected_validity"),
    [
        ({"input_tokens": "bad", "output_tokens": 2}, 0.9, "invalid"),
        ({"input_tokens": True, "output_tokens": 2}, 0.9, "invalid"),
        ({"input_tokens": float("nan"), "output_tokens": 2}, 0.9, "invalid"),
        ({"input_tokens": float("inf"), "output_tokens": 2}, 0.9, "invalid"),
        ({"input_tokens": 2, "output_tokens": 2}, float("nan"), "invalid"),
        ({"input_tokens": 2, "output_tokens": 2}, float("inf"), "invalid"),
        ({"input_tokens": 2, "output_tokens": 2}, 10 ** 1000, "invalid"),
    ],
)
def test_real_http_evaluate_rejects_pathological_provider_values_with_local_fallback(
    tmp_path, monkeypatch, usage, score, expected_validity
):
    _typescript_repo(tmp_path)
    settings = Settings(workspace_roots=(tmp_path,), lexical_enabled=False)

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return json.dumps({
                "answers": {"fit_0": {"noul": score}},
                "usage": usage,
            }, allow_nan=True).encode()

    monkeypatch.setattr("urllib.request.urlopen", lambda *args, **kwargs: Response())
    selector = JevSelector("never-log-this-key", settings)
    result = ContextRouter(settings, selector).route(
        "Refactor src/Root.tsx and identify exact symbols", cwd=tmp_path
    )

    assert result.status == "routed"
    assert result.metrics["transport_status"] == "ok"
    assert result.metrics["response_validity"] == expected_validity
    expected_outcome = "failed" if not isinstance(usage, dict) or not all(
        type(usage.get(field)) is int and 0 <= usage[field] <= 10_000_000
        for field in ("input_tokens", "output_tokens")
    ) else "no-accepted"
    assert result.metrics["external_selection_outcome"] == expected_outcome
    assert result.metrics["fallback_used"] is True
    assert result.metrics["external_selected_ids"] == []
    assert result.metrics["selected_ids"]
    assert "never-log-this-key" not in json.dumps(result.to_dict())
    assert "nan" not in json.dumps(result.to_dict()).lower()
    assert "infinity" not in json.dumps(result.to_dict()).lower()


@pytest.mark.parametrize(
    ("response", "expected_selector_validity"),
    [
        (
            {
                "answers": {"fit_0": {"noul": 0.9}},
                "usage": {"input_tokens": 1, "output_tokens": 1, "unexpected": 0},
            },
            "invalid",
        ),
        (
            {
                "answers": {"fit_0": {"noul": 0.9, "unexpected": 0}},
                "usage": {"input_tokens": 1, "output_tokens": 1},
            },
            "invalid",
        ),
    ],
)
def test_real_http_evaluate_rejects_non_exact_usage_or_answer_mappings_with_local_fallback(
    tmp_path, monkeypatch, response, expected_selector_validity
):
    _typescript_repo(tmp_path)
    settings = Settings(workspace_roots=(tmp_path,), lexical_enabled=False)

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return json.dumps(response).encode()

    monkeypatch.setattr("urllib.request.urlopen", lambda *args, **kwargs: Response())
    selector = JevSelector("never-log-this-key", settings)
    result = ContextRouter(settings, selector).route(
        "Refactor src/Root.tsx and identify exact symbols", cwd=tmp_path
    )

    assert result.status == "routed"
    assert selector.last_response_validity == expected_selector_validity
    assert result.metrics["response_validity"] == expected_selector_validity
    assert result.metrics["external_selection_outcome"] in {"failed", "no-accepted"}
    assert result.metrics["external_selected_ids"] == []
    assert result.metrics["fallback_used"] is True
    assert result.metrics["local_fallback_used"] is True
    assert result.metrics["selected_ids"]


def _typescript_repo(root):
    (root / ".git").mkdir()
    (root / "src").mkdir()
    (root / "src" / "Root.tsx").write_text(
        'export const Root = () => <Composition id="One" />;\n'
    )


@pytest.mark.parametrize("status_code", [401, 403])
def test_http_selector_failure_preserves_safe_status_and_persistent_metrics(tmp_path, monkeypatch, status_code):
    _typescript_repo(tmp_path)
    metrics_path = tmp_path / "router-metrics.jsonl"
    settings = Settings(
        workspace_roots=(tmp_path,), metrics_path=metrics_path, lexical_enabled=False
    )

    def unauthorized(*args, **kwargs):
        raise urllib.error.HTTPError("https://api.typesafe.ai", status_code, "Unauthorized", Message(), None)

    monkeypatch.setattr("urllib.request.urlopen", unauthorized)
    events = []
    result = ContextRouter(settings, JevSelector("never-log-this-key", settings)).route(
        "Refactor src/Root.tsx and identify exact symbols", cwd=tmp_path, progress=events.append
    )

    assert result.status == "routed"
    assert result.metrics["selection_reason"] == "local-shortlist"
    assert result.metrics["selector_error"] == "HTTPError"
    assert result.metrics["selector_error_status_code"] == status_code
    assert result.metrics["selector_error_message"] == f"TypeSafe request failed: HTTPError status={status_code}"
    assert result.metrics["transport_status"] == "failed"
    assert result.metrics["external_selection_outcome"] == "failed"
    assert result.metrics["local_fallback_used"] is True
    assert result.metrics["fallback_used"] is True
    persisted = metrics_path.read_text()
    assert f"HTTPError status={status_code}" in persisted
    assert "never-log-this-key" not in persisted
    assert "https://api.typesafe.ai" not in persisted
    assert "Authorization" not in persisted
    failure_event = next(event for event in events if event.get("selector_error") == "HTTPError")
    assert failure_event["selector_error_status_code"] == status_code
    assert "never-log-this-key" not in json.dumps(events)


def test_invalid_json_selector_failure_preserves_original_type(tmp_path, monkeypatch):
    _typescript_repo(tmp_path)
    settings = Settings(workspace_roots=(tmp_path,), lexical_enabled=False)

    class InvalidJsonResponse:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return b"{not-json"

    monkeypatch.setattr("urllib.request.urlopen", lambda *args, **kwargs: InvalidJsonResponse())
    result = ContextRouter(settings, JevSelector("never-log-this-key", settings)).route(
        "Refactor src/Root.tsx and identify exact symbols", cwd=tmp_path
    )

    assert result.status == "routed"
    assert result.metrics["selector_error"] == "JSONDecodeError"
    assert result.metrics["selector_error_status_code"] is None
    assert result.metrics["transport_status"] == "ok"
    assert result.metrics["response_validity"] == "invalid"
    assert result.metrics["external_status"] == "ok"
    assert result.metrics["external_selection_outcome"] == "failed"
    assert result.metrics["fallback_used"] is True
    assert "never-log-this-key" not in json.dumps(result.to_dict())


@pytest.mark.parametrize(
    ("failure", "expected_type"),
    [
        (TimeoutError("contains-sensitive-timeout-detail"), "TimeoutError"),
        (urllib.error.URLError("contains-sensitive-network-detail"), "URLError"),
    ],
)
def test_transport_selector_failure_is_typed_without_leaking_details(
    tmp_path, monkeypatch, failure, expected_type
):
    _typescript_repo(tmp_path)
    settings = Settings(workspace_roots=(tmp_path,), lexical_enabled=False)

    def fail(*args, **kwargs):
        raise failure

    monkeypatch.setattr("urllib.request.urlopen", fail)
    result = ContextRouter(settings, JevSelector("never-log-this-key", settings)).route(
        "Refactor src/Root.tsx and identify exact symbols", cwd=tmp_path
    )
    serialized = json.dumps(result.to_dict())
    assert result.status == "routed"
    assert result.metrics["selector_error"] == expected_type
    assert result.metrics["selector_error_status_code"] is None
    assert result.metrics["selector_error_message"] == f"TypeSafe request failed: {expected_type}"
    assert result.metrics["external_error_reason"] in {"network", "timeout"}
    assert "contains-sensitive" not in serialized
    assert "never-log-this-key" not in serialized


def test_reranker_zero_accepted_is_transport_ok_semantic_fallback(tmp_path):
    repository = Repository(tmp_path, "demo")
    evidence = {"r": [
        CandidateEvidence("a", "a.py", (1, 1), ("r",), "alpha proof"),
        CandidateEvidence("b", "b.py", (1, 1), ("r",), "beta proof"),
    ]}
    selector = StubJev({"fit_0": {"noul": 0.1}, "fit_1": {"noul": 0.2}})
    selection = selector.rerank_alternatives(
        "choose useful alternative", repository,
        (CoverageRequirement("r", "choose useful alternative"),), evidence, Settings(),
    )
    assert selection.transport_status == "ok"
    assert selection.external_selection_outcome == "no-accepted"
    assert selection.fallback_used is True
    assert selection.reason == "jev-semantic-fallback"
    assert selection.ids == ("a", "b")[:Settings().local_fallback_selected]


def test_reranker_serialized_budget_keeps_round_robin_socle(tmp_path):
    repository = Repository(tmp_path, "demo")
    requirements = tuple(CoverageRequirement(f"r{i}", "requirement " + ("long " * 20)) for i in range(3))
    evidence = {
        requirement.id: [
            CandidateEvidence(f"{requirement.id}-a", f"{requirement.id}-a.py", (1, 1), (requirement.id,), "proof " * 500),
            CandidateEvidence(f"{requirement.id}-b", f"{requirement.id}-b.py", (1, 1), (requirement.id,), "proof " * 500),
        ] for requirement in requirements
    }
    selector = StubJev({f"fit_{index}": {"noul": 0.9} for index in range(6)})
    settings = Settings(selector_max_candidates=6, selector_max_input_tokens=400, candidate_chars=500)
    selection = selector.rerank_alternatives("debug pipeline", repository, requirements, evidence, settings)
    assert selection.estimated_prompt_tokens <= settings.selector_max_input_tokens
    assert selection.candidates_sent <= settings.selector_max_candidates
    assert selection.candidates_sent == 3
