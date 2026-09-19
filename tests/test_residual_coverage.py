"""Synthetic contracts; no external evaluation data or provider calls."""
from dataclasses import replace

import pytest

from jev_context_router.coverage import build_query_plan, coverage_report, evidence_for_symbols
from jev_context_router.models import (RegionProposal, SourceSpan, RenderedEvidence,
                                       RenderedBlock, EvidenceBinding)
from jev_context_router.render import render_context_detailed
from jev_context_router.config import Settings
from jev_context_router.router import ContextRouter
from jev_context_router.providers import LocalSelector
from jev_context_router.errors import ExternalResponseError
from test_structural_parent import structural_index, route_fixture
import jev_context_router.router as router_module


@pytest.mark.parametrize('names', [('copperLatch', 'ivoryRelay'), ('quartzGate', 'violetBridge')])
@pytest.mark.parametrize('kind', ['function', 'text'])
def test_named_union_without_conjunction_or_comment(tmp_path, monkeypatch, names, kind):
    first, second = names
    raw = f'{first} {{ return 1; }}\r\n{second} {{ return 2; }}\r\nunwanted\r\n'.encode()
    split = raw.index(second.encode())
    end = raw.index(b'unwanted')
    index = structural_index(tmp_path, raw, [
        RegionProposal(SourceSpan(0, split), (first,), (kind,)),
        RegionProposal(SourceSpan(split, end), (second,), (kind,)),
    ])
    monkeypatch.setattr(router_module, 'index_repository', lambda *a, **kw: index)
    result = route_fixture(tmp_path, {}, f'Show complete {kind} {first} {second}')
    assert raw[:end].decode() in result.context
    assert 'unwanted' not in result.context
    assert result.status == 'routed'


def test_import_identity_and_continuation_same_obligation(tmp_path, monkeypatch):
    raw = b'bring copperLatch;\ncontinuation ivoryRelay { copperLatch(); }\nunwanted\n'
    split, end = raw.index(b'continuation'), raw.index(b'unwanted')
    index = structural_index(tmp_path, raw, [
        RegionProposal(SourceSpan(0, split), ('copperLatch',), ('import',)),
        RegionProposal(SourceSpan(split, end), ('ivoryRelay',), ('callback',)),
    ])
    monkeypatch.setattr(router_module, 'index_repository', lambda *a, **kw: index)
    result = route_fixture(tmp_path, {}, 'Show copperLatch ivoryRelay')
    assert raw[:end].decode() in result.context
    assert result.status == 'routed'


def test_crossing_blocks_reconstruct_identifier_before_lexical_proof(tmp_path):
    raw = 'é\r\nzeflun\r\nfin\r\n'.encode()
    index = structural_index(tmp_path, raw, [
        RegionProposal(SourceSpan(0, len(raw)), ('frame',), ('object',)),
    ])
    symbol = index.candidates[0]
    split = raw.index(b'lun')
    a, b = raw[:split].decode(), raw[split:].decode()
    rendered = RenderedEvidence(a + '\n' + b, (
        RenderedBlock(symbol.path, symbol.file_sha256, SourceSpan(0, split), 0, len(a), ('parent',)),
        RenderedBlock(symbol.path, symbol.file_sha256, SourceSpan(split, len(raw)), len(a)+1, len(a)+1+len(b), ('child',)),
    ))
    plan = build_query_plan('zeflun')
    binding = EvidenceBinding(plan.requirements[0].id, symbol.id, (SourceSpan(0, len(raw)),))
    report = coverage_report(plan, index.candidates, rendered, evidence_for_symbols(plan, index.candidates), bindings=(binding,))
    assert report.status == 'sufficient'


def test_forged_block_coordinates_cannot_certify_binding(tmp_path):
    raw = b'copperLatch ivoryRelay\n'
    index = structural_index(tmp_path, raw, [RegionProposal(SourceSpan(0, len(raw)), ('frame',), ('object',))])
    symbol = index.candidates[0]
    rendered = render_context_detailed(index.repository, index.candidates, 1000)
    rendered = replace(rendered, context=rendered.context.replace('ivoryRelay', 'xxxxxxxxxx'))
    plan = build_query_plan('Show copperLatch')
    binding = EvidenceBinding(plan.requirements[0].id, symbol.id, (SourceSpan(0, len(raw)),))
    report = coverage_report(plan, index.candidates, rendered, evidence_for_symbols(plan, index.candidates), bindings=(binding,))
    assert report.status == 'insufficient'


@pytest.mark.parametrize('count', [2, 40])
def test_annotation_exclusion_remains_hard_during_parent_closure(tmp_path, monkeypatch, count):
    names = [f'unitMark{i:03}' for i in range(count)]
    raw = ('container {\n' + ''.join(name + '();\n' for name in names) + 'restrictedPayload();\n}\n').encode()
    proposals = [RegionProposal(SourceSpan(0, len(raw)), ('container',), ('object',))]
    for name in names:
        start = raw.index(name.encode())
        proposals.append(RegionProposal(SourceSpan(start, start+len(name)+4), (name,), ('block',)))
    start = raw.index(b'restrictedPayload')
    proposals.append(RegionProposal(SourceSpan(start, raw.index(b'}')), ('quarantined',), ('block',)))
    index = structural_index(tmp_path, raw, proposals)
    monkeypatch.setattr(router_module, 'index_repository', lambda *a, **kw: index)
    result = route_fixture(tmp_path, {}, 'Show ' + ' and '.join(names) + ' excluding quarantined',
                           max_context_chars=16000, max_expanded_symbols=80)
    assert 'restrictedPayload' not in result.context
    assert all(name+'();' in result.context for name in names)
    assert result.status == 'routed'
    assert result.metrics['requirements_planned'] == count


@pytest.mark.parametrize('error,status,validity', [
    (OSError('offline fixture'), 'failed', 'not-applicable'),
    (ExternalResponseError('ValueError'), 'ok', 'invalid'),
])
def test_transport_ledger_uses_observed_failure(tmp_path, error, status, validity):
    class BrokenSelector(LocalSelector):
        def select_symbols(self, *a, **kw):
            raise error
    (tmp_path/'.git').mkdir()
    (tmp_path/'unit.py').write_text('def copperLatch():\n    return 1\n')
    result = ContextRouter(Settings(workspace_roots=(tmp_path,), lexical_enabled=False,
                                   index_cache_enabled=False), BrokenSelector()).route('Show copperLatch', cwd=tmp_path, force=True)
    assert result.status == 'routed'
    for metrics in (result.metrics, result.metrics['external_selector'], result.metrics['ledger']):
        assert metrics['transport_status'] == status
        assert metrics['response_validity'] == validity
    assert result.metrics['ledger']['response_received'] == (status == 'ok')


def test_union_reconstructs_token_across_differently_annotated_regions(tmp_path):
    from jev_context_router.models import CandidateEvidence
    raw = 'é\r\nzeflun\r\n'.encode()
    split = raw.index(b'lun')
    index = structural_index(tmp_path, raw, [
        RegionProposal(SourceSpan(0, split), ('firstHalf',), ('block',)),
        RegionProposal(SourceSpan(split, len(raw)), ('secondHalf',), ('text',)),
    ])
    plan = build_query_plan('zeflun')
    evidence = [CandidateEvidence(s.id, s.path, (s.start_line, s.end_line),
                                  (plan.requirements[0].id,), s.source) for s in index.candidates]
    rendered = render_context_detailed(index.repository, index.candidates, 1000)
    assert coverage_report(plan, index.candidates, rendered, evidence).status == 'sufficient'


def test_fallback_precision_survives_cache_roundtrip(tmp_path):
    from jev_context_router.index import index_repository
    from jev_context_router.models import Repository
    (tmp_path/'unit.py').write_bytes(b'def copperLatch(:\r\n    return 1\r\n')
    repo = Repository(tmp_path, 'synthetic')
    cold = index_repository(repo, cache_path=tmp_path/'cache.json')
    warm = index_repository(repo, cache_path=tmp_path/'cache.json')
    assert warm.stats['index_cache_hit']
    assert cold.regions == warm.regions
    assert all('syntax-fallback' in region.precision for region in warm.regions)


def test_invalid_syntax_precision_is_exposed_for_rendered_proof(tmp_path):
    result = route_fixture(tmp_path, {'broken.py': 'def copperLatch(:\n    return 1\n'}, 'Show copperLatch')
    assert 'copperLatch' in result.context
    blocks = result.metrics['rendered_evidence']['blocks']
    assert blocks
    assert all('syntax-fallback' in block['precision'] for block in blocks)
    assert result.metrics['fallback']['syntax']['used'] is True
