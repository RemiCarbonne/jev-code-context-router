import json
import io
from types import SimpleNamespace
from pathlib import Path

from jev_context_router.cli import command_codex_hook
from jev_context_router.installers import install_claude, install_codex, install_hermes
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


def test_codex_cli_hook_installer_is_idempotent(tmp_path):
    path = install_codex(tmp_path, apply=True, mode="cli")
    assert isinstance(path, Path)
    install_codex(tmp_path, apply=True, mode="cli")
    data = json.loads(path.read_text())
    groups = data["hooks"]["UserPromptSubmit"]
    assert len(groups) == 1
    assert groups[0]["hooks"][0]["command"] == "jev-context codex-hook"
    assert groups[0]["hooks"][0]["timeout"] == 30


def test_codex_cli_hook_routes_typescript_context(tmp_path, monkeypatch, capsys):
    (tmp_path / ".git").mkdir()
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "Root.tsx").write_text(
        'export const Root = () => <Composition id="One" />;\n'
    )
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    monkeypatch.setattr(
        "sys.stdin",
        io.StringIO(json.dumps({
            "hook_event_name": "UserPromptSubmit",
            "cwd": str(tmp_path),
            "prompt": "Refactor src/Root.tsx and identify the exact symbols",
        })),
    )
    assert command_codex_hook(SimpleNamespace(config=None)) == 0
    payload = json.loads(capsys.readouterr().out)
    output = payload["hookSpecificOutput"]
    assert output["hookEventName"] == "UserPromptSubmit"
    assert "src/Root.tsx" in output["additionalContext"]
