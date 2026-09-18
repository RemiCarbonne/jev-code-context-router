import json
from pathlib import Path

from jev_context_router.installers import install_claude, install_hermes
from jev_context_router.providers import sanitize_candidate


def test_candidate_redaction():
    text = "API_KEY='abc123' owner@example.com"
    redacted = sanitize_candidate(text, 1000)
    assert "abc123" not in redacted
    assert "owner@example.com" not in redacted


def test_claude_installer_is_idempotent(tmp_path):
    path = install_claude(tmp_path)
    install_claude(tmp_path)
    data = json.loads(path.read_text())
    assert len(data["hooks"]["UserPromptSubmit"]) == 1
    assert data["hooks"]["UserPromptSubmit"][0]["hooks"][0]["command"] == "jev-context claude-hook"


def test_hermes_installer_vendors_core(tmp_path):
    target = install_hermes(tmp_path)
    assert (target / "plugin.yaml").is_file()
    assert (target / "__init__.py").is_file()
    assert (target / "jev_context_router" / "router.py").is_file()
    assert not list(target.rglob("*.pyc"))
