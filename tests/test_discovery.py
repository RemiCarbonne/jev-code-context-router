from pathlib import Path

from jev_context_router.discovery import discover_repositories, resolve_repository


def make_repo(root: Path, name: str, readme: str = "") -> Path:
    repo = root / name
    (repo / ".git").mkdir(parents=True)
    (repo / "README.md").write_text(readme or f"# {name}\n")
    return repo


def test_discovery_and_explicit_resolution(tmp_path):
    alpha = make_repo(tmp_path, "alpha-api")
    make_repo(tmp_path, "beta-web")
    repos = discover_repositories((tmp_path,))
    selected, reason = resolve_repository("Fix the auth bug in alpha-api", tmp_path, repos)
    assert selected and selected.root == alpha
    assert reason == "explicit-query"


def test_cwd_beats_unrelated_history(tmp_path):
    alpha = make_repo(tmp_path, "alpha")
    make_repo(tmp_path, "beta")
    repos = discover_repositories((tmp_path,))
    selected, reason = resolve_repository("Fix this failing test", alpha / "src", repos, ("Earlier we discussed beta",))
    assert selected and selected.root == alpha
    assert reason == "current-working-directory"


def test_history_is_used_only_for_pure_continuation(tmp_path):
    make_repo(tmp_path, "alpha")
    beta = make_repo(tmp_path, "beta")
    repos = discover_repositories((tmp_path,))
    selected, reason = resolve_repository("continue", tmp_path, repos, ("Fix beta",))
    assert selected and selected.root == beta
    assert reason == "continuation-user-history"
    selected, reason = resolve_repository("Fix this code", tmp_path, repos, ("Earlier beta",))
    assert selected is None
    assert reason == "unresolved"


def test_repository_description_never_reads_a_symlink_outside_root(tmp_path):
    workspace = tmp_path / "workspace"
    repository = workspace / "safe"
    repository.mkdir(parents=True)
    (repository / ".git").mkdir()
    secret = tmp_path / "outside-secret.txt"
    secret.write_text("DO-NOT-READ")
    (repository / "README.md").symlink_to(secret)

    [discovered] = discover_repositories((workspace,))

    assert discovered.description == ""
