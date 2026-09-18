from pathlib import Path

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
