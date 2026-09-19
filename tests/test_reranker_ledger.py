from dataclasses import replace

import pytest

from jev_context_router.config import Settings
from jev_context_router.models import CandidateEvidence, CoverageRequirement, Repository
from jev_context_router.providers import JevSelector, normalize_alternatives, serialize_request


class PayloadStub(JevSelector):
    def __init__(self, settings, mode='low'):
        super().__init__('', settings)
        self.mode = mode
        self.calls = []

    def _evaluate(self, state, questions):
        self.calls.append(serialize_request(state, questions, self.settings))
        keys = list(questions)
        if self.mode == 'partial':
            keys = keys[:1]
        return {k: {'noul': 0 if self.mode == 'low' else 1} for k in keys}, {'input_tokens': 0, 'output_tokens': 0}, 0.0


def alternatives(count=2):
    return {rid: [CandidateEvidence(str(i), f'{i}.py', (1, 2), (rid,), 'source', 'proof') for i in range(count)] for rid in ('r', 's')}


def requirements():
    return tuple(CoverageRequirement(r, r) for r in ('r', 's'))


def test_normalization_merges_bindings_and_detects_inconsistent_ids():
    items = normalize_alternatives(alternatives())
    assert len(items) == 2
    assert all(e.requirement_ids == ('r', 's') for e in items.values())
    bad = alternatives()
    bad['s'][0] = replace(bad['s'][0], path='other.py')
    with pytest.raises(ValueError):
        normalize_alternatives(bad)


def test_shared_reranker_candidates_scored_once_and_rejection_is_explicit(tmp_path):
    settings = Settings(selector_max_candidates=80, selector_max_input_bytes=100000)
    stub = PayloadStub(settings)
    result = stub.rerank_alternatives('request', Repository(tmp_path, 'demo'), requirements(), alternatives(80), settings)
    assert len(result.ledger['sent']) == 80
    assert len(set(result.ledger['sent'])) == 80
    assert result.ledger['score_valid'] == result.ledger['sent']
    assert result.external_selected_ids == ()
    assert result.response_validity == 'valid'
    assert result.transport_status == 'ok'
    assert result.external_selection_outcome == 'no-accepted'
    assert result.semantic_fallback_reason == 'no-external-candidate-above-threshold'
    assert result.sent_payload_bytes == len(stub.calls[0])


def test_partial_then_valid_same_instance(tmp_path):
    settings = Settings()
    stub = PayloadStub(settings, 'partial')
    first = stub.rerank_alternatives('request', Repository(tmp_path, 'demo'), requirements(), alternatives(), settings)
    assert first.response_validity == 'partial'
    assert len(first.ledger['score_missing']) == 1
    stub.mode = 'high'
    second = stub.rerank_alternatives('request', Repository(tmp_path, 'demo'), requirements(), alternatives(), settings)
    assert second.response_validity == 'valid'
    assert not second.ledger['score_missing']


def test_impossible_budget_and_settings_mismatch_never_call(tmp_path):
    settings = Settings(selector_max_input_bytes=1)
    stub = PayloadStub(settings)
    result = stub.rerank_alternatives('雪', Repository(tmp_path, 'demo'), requirements(), alternatives(), settings)
    assert not stub.calls
    assert result.sent_payload_bytes == 0
    assert result.reason == 'selector-budget-local-fallback'
    assert not result.ledger['sent']
    result = stub.rerank_alternatives('雪', Repository(tmp_path, 'demo'), requirements(), alternatives(), replace(settings, selector_max_input_bytes=100000))
    assert not stub.calls


def test_local_base_does_not_depend_on_external_rejection(tmp_path):
    from jev_context_router.router import ContextRouter
    from jev_context_router.providers import LocalSelector
    (tmp_path / '.git').mkdir()
    (tmp_path / 'a.py').write_text('def normalize_path(value):\n    return value.rstrip("/")\n\ndef normalize_other(value):\n    return value\n')
    settings = Settings(workspace_roots=(tmp_path,), lexical_enabled=False, index_cache_enabled=False)
    local = ContextRouter(settings, LocalSelector()).route('Refactor normalize_path function', cwd=tmp_path)
    low = ContextRouter(settings, PayloadStub(settings)).route('Refactor normalize_path function', cwd=tmp_path)
    assert local.metrics['ledger']['local_base_ids'] == low.metrics['ledger']['local_base_ids']
    for result in (local, low):
        ledger = result.metrics['ledger']
        assert set(ledger['local_base_ids']) <= set(ledger['final_selected_ids'])
        assert len(ledger['dispositions']) == len(ledger['considered_ids'])


def test_evaluate_boundary_guard_applies_to_repository_selection(tmp_path):
    selector = JevSelector('', Settings(selector_max_input_bytes=0))
    with pytest.raises(ValueError, match='selector-budget-local-fallback'):
        selector.choose_repository('雪', [Repository(tmp_path, 'demo')], 0.5)
