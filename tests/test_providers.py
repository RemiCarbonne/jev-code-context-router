import json
import urllib.error
from email.message import Message
from pathlib import Path

import pytest
from jev_context_router.config import Settings
from jev_context_router.models import Repository, Symbol
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
    persisted = metrics_path.read_text()
    assert f"HTTPError status={status_code}" in persisted
    assert "never-log-this-key" not in persisted
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
