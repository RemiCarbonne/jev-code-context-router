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
