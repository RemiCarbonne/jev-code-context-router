import os
from pathlib import Path

from jev_context_router.discovery import Repository
from jev_context_router.index import index_repository
from jev_context_router.security import PathPolicy


def test_python_and_typescript_indexing(tmp_path):
    (tmp_path / "service.py").write_text("class BillingService:\n    def total(self, amount):\n        return amount\n")
    (tmp_path / "web.ts").write_text("export function formatTotal(value: number) {\n  return String(value);\n}\n")
    repo = Repository(tmp_path, "demo", languages=("python", "typescript"))
    index = index_repository(repo)
    assert any(symbol.name == "BillingService" and symbol.language == "python" for symbol in index.symbols)
    assert any(symbol.name == "formatTotal" and symbol.language == "typescript" for symbol in index.symbols)


def test_secret_files_and_escaping_symlinks_are_excluded_without_hiding_auth_code(tmp_path):
    (tmp_path / "service.py").write_text("def safe(): return True\n")
    (tmp_path / "auth.py").write_text("def authenticate(): return True\n")
    (tmp_path / "credentials.json").write_text('{"token":"secret"}')
    outside = tmp_path.parent / "outside-router-test.py"
    outside.write_text("def outside(): return True\n")
    try:
        os.symlink(outside, tmp_path / "escape.py")
        policy = PathPolicy()
        index = index_repository(Repository(tmp_path, "demo"), policy)
        paths = {symbol.path for symbol in index.symbols}
        assert "service.py" in paths
        assert "auth.py" in paths
        assert not policy.allows(tmp_path, tmp_path / "credentials.json")
        assert "escape.py" not in paths
    finally:
        outside.unlink(missing_ok=True)


def test_partial_index_reads_only_allowed_included_paths(tmp_path):
    selected = tmp_path / "selected.ts"
    unrelated = tmp_path / "unrelated.ts"
    secret = tmp_path / ".env.ts"
    selected.write_text("export const selectedHandler = () => true;\n")
    unrelated.write_text("export const unrelatedHandler = () => false;\n")
    secret.write_text("export const leakedSecret = 'no';\n")

    index = index_repository(
        Repository(tmp_path, "demo"),
        include_paths=(selected, unrelated, secret),
        max_files=10,
    )

    assert index.stats["index_mode"] == "partial"
    assert index.stats["files_indexed"] == 2
    assert index.stats["candidate_files"] == ["selected.ts", "unrelated.ts"]
    assert {symbol.path for symbol in index.symbols} == {"selected.ts", "unrelated.ts"}


def test_generated_out_directory_is_pruned(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.ts").write_text("export const main = () => true;\n")
    (tmp_path / "out" / "vendored-runtime").mkdir(parents=True)
    (tmp_path / "out" / "vendored-runtime" / "generated.ts").write_text(
        "export const generated = () => false;\n"
    )
    index = index_repository(Repository(tmp_path, "demo"))
    assert index.stats["files_indexed"] == 1
    assert {symbol.path for symbol in index.symbols} == {"src/main.ts"}
