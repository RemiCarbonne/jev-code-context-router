import json
import subprocess
import time
from pathlib import Path

import jev_context_router.router as router_module
from jev_context_router.config import Settings
from jev_context_router.discovery import Repository
from jev_context_router.index import index_repository
from jev_context_router.models import Selection
from jev_context_router.router import ContextRouter


QUERY = (
    "Refactor src/Root.tsx to eliminate repeated Remotion Composition declarations while "
    "preserving every composition id, component, duration, fps, dimensions, and defaultProps. "
    "Identify the exact repository files and symbols needed to implement and validate this "
    "refactor. Do not edit files."
)


def make_typescript_repo(root: Path) -> None:
    (root / ".git").mkdir()
    (root / "src").mkdir()
    (root / "src" / "Root.tsx").write_text(
        'import { Composition } from "remotion";\n'
        "export const Root = () => (<>\n"
        '  <Composition id="One" component={One} durationInFrames={30} fps={30} width={1920} height={1080} />\n'
        "</>);\n"
    )
    dependency = root / "node_modules" / "large-package"
    dependency.mkdir(parents=True)
    for index in range(40):
        (dependency / f"generated-{index}.ts").write_text("export const ignored = 1;\n")


def test_index_prunes_dependencies_before_visiting_files(tmp_path):
    make_typescript_repo(tmp_path)
    index = index_repository(Repository(tmp_path, "demo"))
    assert index.stats["files_seen"] == 1
    assert index.stats["files_indexed"] == 1
    assert index.stats["candidate_files"] == ["src/Root.tsx"]


class SleepingSelector:
    def choose_repository(self, query, repositories, threshold):
        return None, {}

    def select_symbols(self, query, repository, candidates, settings):
        time.sleep(1)
        return Selection(tuple(symbol.id for symbol in candidates[:1]), reason="late")


def test_external_selection_timeout_is_structured_and_bounded(tmp_path):
    make_typescript_repo(tmp_path)
    settings = Settings(
        workspace_roots=(tmp_path,), route_timeout_seconds=0.15,
        external_timeout_seconds=0.05, lexical_enabled=False,
    )
    started = time.perf_counter()
    result = ContextRouter(settings, SleepingSelector()).route(QUERY, cwd=tmp_path)
    elapsed = time.perf_counter() - started
    assert elapsed < 0.5
    assert result.status == "routed"
    assert result.metrics["selector_error"] == "RoutingTimeout"
    assert result.metrics["fallback_used"] is True
    assert result.metrics["seconds"] < 0.5


def test_indexing_timeout_is_structured_and_bounded(tmp_path, monkeypatch):
    make_typescript_repo(tmp_path)

    def stalled_index(*args, **kwargs):
        time.sleep(1)

    monkeypatch.setattr(router_module, "index_repository", stalled_index)
    settings = Settings(
        workspace_roots=(tmp_path,), route_timeout_seconds=0.15,
        index_timeout_seconds=0.05,
    )
    started = time.perf_counter()
    result = ContextRouter(settings).route(QUERY, cwd=tmp_path)
    assert time.perf_counter() - started < 0.5
    assert result.status == "timeout"
    assert result.metrics["timeout_stage"] == "indexing"


def test_unexpected_index_error_contains_original_traceback(tmp_path, monkeypatch):
    make_typescript_repo(tmp_path)

    def broken_index(*args, **kwargs):
        raise ValueError("synthetic index failure")

    monkeypatch.setattr(router_module, "index_repository", broken_index)
    result = ContextRouter(Settings(workspace_roots=(tmp_path,))).route(QUERY, cwd=tmp_path)
    assert result.status == "error"
    assert result.metrics["error_stage"] == "indexing"
    assert result.metrics["error_type"] == "ValueError"
    assert "synthetic index failure" in result.metrics["traceback"]


def test_typescript_cli_returns_json_and_debug_events(tmp_path):
    make_typescript_repo(tmp_path)
    command = [
        "jev-context", "route", "--cwd", str(tmp_path), "--format", "json",
        "--debug", "--timeout", "5", QUERY,
    ]
    completed = subprocess.run(command, capture_output=True, text=True, timeout=10)
    assert completed.returncode == 0
    payload = json.loads(completed.stdout)
    assert payload["status"] == "routed"
    assert payload["metrics"]["seconds"] < 5
    assert payload["metrics"]["files_indexed"] == 1
    assert payload["metrics"]["context_bytes"] > 0
    assert payload["metrics"]["estimated_context_tokens"] > 0
    assert payload["metrics"]["cache"]["status"] == "disabled"
    assert "src/Root.tsx" in payload["metrics"]["candidate_files"]
    events = [json.loads(line) for line in completed.stderr.splitlines() if line.strip()]
    assert any(event["stage"] == "indexing" for event in events)
    assert events[-1]["stage"] == "complete"
