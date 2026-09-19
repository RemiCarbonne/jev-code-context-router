from pathlib import Path
import shutil

import pytest

from jev_context_router.config import Settings
from jev_context_router.models import Repository, Selection
from jev_context_router.router import ContextRouter


class FakeSelector:
    def choose_repository(self, query, repositories, threshold):
        match = next((repo for repo in repositories if repo.name == "beta"), None)
        return match, {"provider": "fake", "best": 0.9}

    def select_symbols(self, query, repository, candidates, settings):
        return Selection(tuple(symbol.id for symbol in candidates[:1]), reason="fake")


def make_repo(root: Path, name: str, source: str) -> Path:
    repo = root / name
    (repo / ".git").mkdir(parents=True)
    (repo / "service.py").write_text(source)
    return repo


def test_routes_current_repository_without_hardcoded_aliases(tmp_path):
    repo = make_repo(tmp_path, "random-project", "class PaymentService:\n    def refund(self): return True\n")
    settings = Settings(workspace_roots=(tmp_path,), max_context_chars=6000)
    result = ContextRouter(settings, FakeSelector()).route("Fix PaymentService refund bug", cwd=repo)
    assert result.status == "routed"
    assert result.repository and result.repository.root == repo
    assert "PaymentService" in result.context


def test_semantic_repository_selection_when_cwd_is_workspace(tmp_path):
    make_repo(tmp_path, "alpha", "def unrelated(): return 1\n")
    beta = make_repo(tmp_path, "beta", "def repair_invoice(): return 2\n")
    result = ContextRouter(Settings(workspace_roots=(tmp_path,)), FakeSelector()).route("Fix the invoice code", cwd=tmp_path)
    assert result.status == "routed"
    assert result.repository and result.repository.root == beta


def test_non_code_turn_does_not_index(tmp_path):
    make_repo(tmp_path, "alpha", "def hello(): return 1\n")
    result = ContextRouter(Settings(workspace_roots=(tmp_path,)), FakeSelector()).route("Explain our sales strategy", cwd=tmp_path)
    assert result.status == "not-code"
    assert not result.context


def test_unresolved_repository_does_not_guess_from_history(tmp_path):
    make_repo(tmp_path, "alpha", "def alpha(): return 1\n")
    make_repo(tmp_path, "beta", "def beta(): return 1\n")
    class NoChoice(FakeSelector):
        def choose_repository(self, query, repositories, threshold): return None, {"provider": "fake"}
    result = ContextRouter(Settings(workspace_roots=(tmp_path,)), NoChoice()).route(
        "Fix this Python bug", cwd=tmp_path, recent_user_messages=("Yesterday: alpha",)
    )
    assert result.status == "repository-unresolved"
    assert not result.context


class CountingSelector(FakeSelector):
    def __init__(self):
        self.symbol_calls = 0

    def select_symbols(self, query, repository, candidates, settings):
        self.symbol_calls += 1
        return super().select_symbols(query, repository, candidates, settings)


def make_typescript_repo(root: Path) -> Path:
    repo = root / "doctolib-project"
    (repo / ".git").mkdir(parents=True)
    (repo / "src").mkdir()
    (repo / "src" / "doctolib.ts").write_text(
        "export const healthcareProviders = ORGANIZATION;\n"
        "export const city = 'marseille';\n"
        "export const estimatedPractitioners = getPhoneNumber();\n"
        "export const save = () => writeFileSync('providers.json', healthcareProviders);\n"
    )
    (repo / "src" / "other.ts").write_text(
        "export const saveOther = () => writeFileSync('other.json', '{}');\n"
    )
    return repo


@pytest.mark.skipif(shutil.which("rg") is None, reason="ripgrep is optional")
def test_high_confidence_lexical_route_skips_external_selector(tmp_path):
    repo = make_typescript_repo(tmp_path)
    selector = CountingSelector()
    settings = Settings(workspace_roots=(tmp_path,), max_context_chars=6000)
    result = ContextRouter(settings, selector).route(
        "Refactor healthcareProviders ORGANIZATION marseille estimatedPractitioners "
        "writeFileSync getPhoneNumber without editing files",
        cwd=repo,
    )
    assert result.status == "routed"
    assert selector.symbol_calls == 0
    assert result.metrics["retrieval_mode"] == "rg-fast-path"
    assert result.metrics["jev_skipped"] is True
    assert result.metrics["index_mode"] == "partial"
    assert result.metrics["lexical_matched_files"] == 2
    assert "src/doctolib.ts" in result.metrics["included_files"]


def test_broad_single_identifier_keeps_structural_selector(tmp_path):
    repo = make_typescript_repo(tmp_path)
    selector = CountingSelector()
    result = ContextRouter(Settings(workspace_roots=(tmp_path,)), selector).route(
        "Refactor every writeFileSync call without changing behavior", cwd=repo
    )
    assert result.status == "routed"
    assert selector.symbol_calls == 1
    assert result.metrics["retrieval_mode"] == "structural-full"
    assert result.metrics["jev_skipped"] is False
    assert result.metrics["lexical_fallback_reason"] == "insufficient-confidence"
    assert set(result.metrics) >= {
        "intent", "index", "lexical", "ranking", "external_selector",
        "context", "fallback", "cache", "total",
    }
    assert result.metrics["context"]["included_files"] == result.metrics["included_files"]


@pytest.mark.skipif(shutil.which("rg") is None, reason="ripgrep is optional")
def test_persistent_fast_path_metrics_do_not_store_query_terms(tmp_path):
    repo = make_typescript_repo(tmp_path)
    metrics_path = tmp_path / "metrics.jsonl"
    settings = Settings(workspace_roots=(tmp_path,), metrics_path=metrics_path)
    ContextRouter(settings, CountingSelector()).route(
        "Refactor healthcareProviders ORGANIZATION marseille getPhoneNumber privatePromptMarker",
        cwd=repo,
    )
    persisted = metrics_path.read_text()
    assert '"retrieval_mode": "rg-fast-path"' in persisted
    assert "privatePromptMarker" not in persisted
    assert '"query"' not in persisted
