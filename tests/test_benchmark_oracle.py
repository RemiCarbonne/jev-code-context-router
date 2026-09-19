import importlib.util
from pathlib import Path

import pytest

from jev_context_router.config import Settings


def load_stub():
    spec = importlib.util.spec_from_file_location('offline_stub', Path(__file__).parents[1] / 'benchmarks/selector_stubs.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.OfflineSelector


def test_offline_stubs_only_use_payload_order():
    cls = load_stub()
    for mode in ('all-low', 'all-high', 'partial', 'invalid', 'reverse-order'):
        stub = cls(Settings(), mode)
        scores, usage, _ = stub._evaluate({'alternatives': {'0': {'id': 'a'}, '1': {'id': 'b'}}}, {'fit_0': {}, 'fit_1': {}})
        assert stub.captured
        assert stub.transport_calls == 1
        assert len(scores) == (1 if mode == 'partial' else 2)
        assert usage == {'input_tokens': 0, 'output_tokens': 0}
    stub = cls(Settings(), 'unavailable')
    with pytest.raises(OSError, match='synthetic'):
        stub._evaluate({}, {})
    assert stub.captured == b''
