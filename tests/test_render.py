from jev_context_router.models import Repository, Symbol
from jev_context_router.render import render_context


def test_required_symbol_is_line_bounded_when_context_budget_is_tight(tmp_path):
    repository = Repository(tmp_path, "demo")
    symbol = Symbol(
        "service.py::run", "service.py", "run", "run", "function", "python",
        1, 20, "def run():\n" + "    value = True\n" * 18 + "    return value",
    )
    context, included = render_context(repository, [symbol], 500, required_ids=(symbol.id,))
    assert symbol.id in included
    assert "<symbol" in context and "</symbol>" in context
    assert "def run():" in context
    assert "return value" not in context


def test_empty_symbols_are_not_rendered_and_manifest_remains_valid(tmp_path):
    repository = Repository(tmp_path, "demo")
    empty = Symbol("empty.py::empty", "empty.py", "empty", "empty", "function", "python", 1, 1, "")
    context, included = render_context(repository, [empty], 2_000)
    assert included == ()
    assert "empty.py::empty" not in context
    assert "<context_manifest>" in context
