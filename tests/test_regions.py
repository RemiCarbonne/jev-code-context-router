from itertools import permutations

import pytest

from jev_context_router.models import SourceSpan, RegionProposal
from jev_context_router.regions import canonicalize, union_spans, source_text


def proposal(start, end, name='a'):
    return RegionProposal(SourceSpan(start, end), names=(name,), provenance=('test',))


def test_identity_annotations_and_adapter_order():
    proposals = [proposal(0, 5, 'a'), proposal(0, 5, 'b'), proposal(1, 3, 'c')]
    outputs = [canonicalize('a.flux', b'abcdef', p) for p in permutations(proposals)]
    assert all(result == outputs[0] for result in outputs)
    regions = outputs[0]
    assert len(regions) == 2
    parent = next(r for r in regions if r.span == SourceSpan(0, 5))
    child = next(r for r in regions if r.span == SourceSpan(1, 3))
    assert parent.names == ('a', 'b')
    assert child.parent_id == parent.region_id
    assert parent.children_ids == (child.region_id,)
    assert canonicalize('a.flux', b'abcdef', [proposal(0, 5, 'z')])[0].region_id == parent.region_id


def test_crossing_and_smallest_parent():
    regions = canonicalize('a', b'0123456789', [proposal(0, 10), proposal(1, 6), proposal(4, 8), proposal(4, 5)])
    by_span = {r.span: r for r in regions}
    left, right, child = (by_span[SourceSpan(*s)] for s in [(1, 6), (4, 8), (4, 5)])
    assert right.region_id in left.overlaps
    assert left.region_id in right.overlaps
    assert child.parent_id == right.region_id
    assert not child.overlaps
    assert union_spans([left.span, right.span, child.span]) == (SourceSpan(1, 8),)


def test_exact_utf8_crlf_and_snapshot_identity():
    raw = 'é\r\n雪\r\n'.encode()
    region = canonicalize('a', raw, [proposal(0, len(raw))])[0]
    assert source_text(region, raw) == raw.decode()
    assert region.span.start_line == 1
    assert region.span.end_line == 2
    assert canonicalize('b', raw, [proposal(0, len(raw))])[0].region_id != region.region_id
    assert canonicalize('a', raw + b'!', [proposal(0, len(raw))])[0].region_id != region.region_id
    with pytest.raises(ValueError):
        source_text(region, raw + b'!')


def test_identical_text_at_different_positions_remains_distinct():
    regions = canonicalize('a', b'aa aa', [proposal(0, 2), proposal(3, 5)])
    assert len({r.region_id for r in regions}) == 2
    assert union_spans([r.span for r in regions]) == (SourceSpan(0, 2), SourceSpan(3, 5))


@pytest.mark.parametrize('start,end', [(-1, 2), (2, 1), (0, 0), (0, 99), (1, 2)])
def test_invalid_intervals_rejected(start, end):
    with pytest.raises(ValueError):
        canonicalize('a', 'éabc'.encode(), [proposal(start, end)])


def test_union_does_not_double_bill_and_coalesces_adjacency():
    assert union_spans([SourceSpan(0, 3), SourceSpan(2, 4), SourceSpan(4, 7)]) == (SourceSpan(0, 7),)


def test_network_guard_is_active():
    import socket
    with pytest.raises(AssertionError, match='network disabled'):
        socket.create_connection(('192.0.2.1', 443))
