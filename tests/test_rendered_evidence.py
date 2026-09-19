import pytest

from jev_context_router.models import Repository, CoverageRequirement, QueryPlan
from jev_context_router.index import index_repository
from jev_context_router.render import render_context_detailed
from jev_context_router.coverage import coverage_report, evidence_for_symbols


def test_render_union_and_exact_coordinates(tmp_path):
    raw = '@decorate\r\nclass Café:\r\n    def run(self):\r\n        return "雪"\r\n'.encode()
    (tmp_path / 'a.py').write_bytes(raw)
    index = index_repository(Repository(tmp_path, 'demo'))
    result = render_context_detailed(index.repository, index.candidates, 4000)
    spans = [(b.span.start_byte, b.span.end_byte) for b in result.blocks]
    assert sum(b-a for a,b in spans) == len(raw)
    assert len(result.blocks) == 1
    assert set(result.complete_ids) | set(result.represented_ids) == {s.id for s in index.candidates}
    for block in result.blocks:
        assert result.context[block.context_start:block.context_end].encode() == raw[block.span.start_byte:block.span.end_byte]


@pytest.mark.parametrize('budget', [0, 1, 32, 100, 200, 400, 800])
def test_strict_budget_and_no_partial_line(tmp_path, budget):
    (tmp_path / 'a.flux').write_text('x' * 900 + '\n')
    index = index_repository(Repository(tmp_path, 'demo'), admitted_globs=('*.flux',))
    result = render_context_detailed(index.repository, index.candidates, budget, required_ids=tuple(s.id for s in index.candidates))
    assert len(result.context) <= budget
    assert not result.blocks
    assert result.packing_losses


def test_coverage_only_uses_rendered_source(tmp_path):
    (tmp_path / 'handling.flux').write_text('normalization policy exception\n' + 'padding\n' * 150 + 'handling\n')
    index = index_repository(Repository(tmp_path, 'demo'), admitted_globs=('*.flux',))
    requirement = CoverageRequirement('r', 'normalization policy exception handling', expected_terms=('normalization', 'policy', 'exception', 'handling'), mandatory_terms=('normalization', 'policy', 'exception', 'handling'))
    plan = QueryPlan(requirement.source, 'localized', (requirement,))
    result = render_context_detailed(index.repository, index.candidates, 300, required_ids=tuple(s.id for s in index.candidates))
    assert 'handling\n' not in result.context
    report = coverage_report(plan, index.candidates, result, evidence_for_symbols(plan, index.candidates))
    assert report.status == 'insufficient'
    full = render_context_detailed(index.repository, index.candidates, 5000)
    assert coverage_report(plan, index.candidates, full, evidence_for_symbols(plan, index.candidates)).status == 'sufficient'


def test_child_proof_represented_by_parent(tmp_path):
    (tmp_path / 'a.py').write_text('class Parent:\n    def child(self):\n        return "normalization handling"\n')
    index = index_repository(Repository(tmp_path, 'demo'))
    plan = QueryPlan('normalization handling', 'localized', (CoverageRequirement('r', 'normalization handling', expected_terms=('normalization', 'handling'), mandatory_terms=('normalization', 'handling')),))
    result = render_context_detailed(index.repository, index.candidates, 1000)
    assert len(result.blocks) == 1
    assert coverage_report(plan, index.candidates, result, evidence_for_symbols(plan, index.candidates)).status == 'sufficient'
