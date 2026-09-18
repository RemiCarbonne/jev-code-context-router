from pathlib import Path

from jev_context_router.config import Settings
from jev_context_router.models import Repository, Symbol
from jev_context_router.providers import JevSelector


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
        "fit_0": {"noul": 0.95}, "current_0": {"noul": 0.9},
        "fit_1": {"noul": 0.1}, "current_1": {"noul": 0.9},
    })
    selection = selector.select_symbols("fix run", repository, symbols, Settings())
    assert selection.ids == ("service.py::run",)
    sent = selector.state["candidate_symbols"]["0"]["source"]
    assert "very-secret" not in sent
    assert "[REDACTED]" in sent


def test_empty_jev_symbol_selection_falls_back_locally(tmp_path):
    repository = Repository(tmp_path, "demo")
    symbols = [Symbol(
        id="a.py::a", path="a.py", name="a", qualname="a", kind="function",
        language="python", start_line=1, end_line=1, source="def a(): pass",
    )]
    selector = StubJev({"fit_0": {"noul": 0.1}, "current_0": {"noul": 0.9}})
    selection = selector.select_symbols("fix it", repository, symbols, Settings())
    assert selection.ids == ("a.py::a",)
    assert selection.reason == "jev-local-fallback"
