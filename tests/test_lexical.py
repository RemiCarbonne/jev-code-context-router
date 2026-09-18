from pathlib import Path
import shutil
import subprocess

import pytest

from jev_context_router.config import Settings
from jev_context_router.lexical import extract_lexical_terms, retrieve_lexical_candidates
from jev_context_router.models import Repository
from jev_context_router.security import PathPolicy


def make_repo(root: Path) -> Repository:
    (root / ".git").mkdir()
    (root / "src").mkdir()
    (root / "src" / "doctolib.ts").write_text(
        "const healthcareProviders = ORGANIZATION;\n"
        "const city = 'marseille';\n"
        "const estimatedPractitioners = getPhoneNumber();\n"
        "writeFileSync('providers.json', JSON.stringify(healthcareProviders));\n"
    )
    (root / "src" / "other.ts").write_text("writeFileSync('other.json', '{}');\n")
    return Repository(root, root.name)


def settings(root: Path) -> Settings:
    return Settings(
        workspace_roots=(root,), lexical_enabled=True,
        lexical_timeout_seconds=0.5, lexical_max_files=20,
        lexical_min_distinct_terms=2, lexical_min_margin=1,
    )


def test_extracts_paths_and_discriminating_identifiers():
    terms, explicit = extract_lexical_terms(
        "Refactor src/doctolib.ts using healthcareProviders ORGANIZATION marseille "
        "estimatedPractitioners writeFileSync getPhoneNumber without editing files"
    )
    assert "src/doctolib.ts" in explicit
    assert {
        "healthcareProviders", "ORGANIZATION", "marseille",
        "estimatedPractitioners", "writeFileSync", "getPhoneNumber",
    } <= set(terms)
    assert "without" not in terms


@pytest.mark.skipif(shutil.which("rg") is None, reason="ripgrep is optional")
def test_multiple_terms_concentrated_in_one_file_are_high_confidence(tmp_path):
    repository = make_repo(tmp_path)
    result = retrieve_lexical_candidates(
        repository,
        "Refactor healthcareProviders ORGANIZATION marseille estimatedPractitioners "
        "writeFileSync getPhoneNumber",
        settings(tmp_path),
        PathPolicy(),
    )
    assert result.high_confidence
    assert result.reason == "distinctive-term-concentration"
    assert result.paths[0].relative_to(tmp_path).as_posix() == "src/doctolib.ts"
    assert result.top_score >= 5
    assert result.matched_files == 2


@pytest.mark.skipif(shutil.which("rg") is None, reason="ripgrep is optional")
def test_single_shared_identifier_falls_back(tmp_path):
    repository = make_repo(tmp_path)
    result = retrieve_lexical_candidates(
        repository, "Find every writeFileSync call", settings(tmp_path), PathPolicy()
    )
    assert not result.high_confidence
    assert result.reason == "insufficient-confidence"
    assert result.matched_files == 2


@pytest.mark.skipif(shutil.which("rg") is None, reason="ripgrep is optional")
def test_explicit_allowed_source_path_is_high_confidence(tmp_path):
    repository = make_repo(tmp_path)
    result = retrieve_lexical_candidates(
        repository, "Refactor src/doctolib.ts without editing", settings(tmp_path), PathPolicy()
    )
    assert result.high_confidence
    assert result.reason == "explicit-source-path"
    assert [path.relative_to(tmp_path).as_posix() for path in result.paths] == ["src/doctolib.ts"]


@pytest.mark.skipif(shutil.which("rg") is None, reason="ripgrep is optional")
def test_query_metacharacters_are_literals_not_shell(tmp_path):
    repository = make_repo(tmp_path)
    marker = tmp_path / "owned"
    result = retrieve_lexical_candidates(
        repository,
        f"Refactor healthcareProviders $(touch {marker}) ; touch {marker}",
        settings(tmp_path),
        PathPolicy(),
    )
    assert not marker.exists()
    assert result.status in {"matched", "no-match"}


def test_missing_ripgrep_returns_safe_fallback(tmp_path, monkeypatch):
    repository = make_repo(tmp_path)
    monkeypatch.setattr("jev_context_router.lexical.shutil.which", lambda _: None)
    result = retrieve_lexical_candidates(
        repository, "Refactor healthcareProviders getPhoneNumber", settings(tmp_path), PathPolicy()
    )
    assert not result.high_confidence
    assert result.status == "unavailable"
    assert result.reason == "ripgrep-unavailable"


def test_explicit_path_traversal_is_not_rewritten_inside_repository(tmp_path):
    repository = make_repo(tmp_path)
    (tmp_path / "secret.py").write_text("def should_not_be_selected(): pass\n")
    result = retrieve_lexical_candidates(
        repository, "Refactor ../../secret.py", settings(tmp_path), PathPolicy()
    )
    assert not result.high_confidence
    assert not result.paths


def test_ripgrep_timeout_returns_safe_fallback(tmp_path, monkeypatch):
    repository = make_repo(tmp_path)
    monkeypatch.setattr("jev_context_router.lexical.shutil.which", lambda _: "/usr/bin/rg")

    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(args[0], timeout=0.01)

    monkeypatch.setattr("jev_context_router.lexical.subprocess.run", timeout)
    result = retrieve_lexical_candidates(
        repository, "Refactor healthcareProviders getPhoneNumber", settings(tmp_path), PathPolicy()
    )
    assert not result.high_confidence
    assert result.status == "timeout"
    assert result.reason == "ripgrep-timeout"
