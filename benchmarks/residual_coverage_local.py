"""Local synthetic regression extension; no independent evaluation inputs."""
from __future__ import annotations

import argparse
import importlib.util
from pathlib import Path


def run(output):
    spec = importlib.util.spec_from_file_location(
        'structural_local', Path(__file__).with_name('structural_parent_local.py'))
    assert spec is not None and spec.loader is not None
    harness = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(harness)
    previous_cases = harness.cases

    def cases():
        return previous_cases() + [
            ('named-conjunctive-union', {'unit.py':
                'def copperLatch():\r\n    return "é"\r\n\r\n'
                'def ivoryRelay():\r\n    return "雪"\r\n\r\n'
                'def unusedSpindle():\r\n    return 23\r\n'},
             'Show complete function copperLatch ivoryRelay', 4000, 'routed',
             ('def copperLatch', 'return "é"', 'def ivoryRelay', 'return "雪"'), ('unusedSpindle',)),
            ('renamed-conjunctive-union', {'unit.py':
                'def quartzGate():\n    return 3\n\n'
                'def violetBridge():\n    return 7\n\n'
                'def unusedSpindle():\n    return 23\n'},
             'Show complete function quartzGate violetBridge', 4000, 'routed',
             ('def quartzGate', 'return 3', 'def violetBridge', 'return 7'), ('unusedSpindle',)),
            ('invalid-syntax-useful-evidence', {'broken.py':
                'def copperLatch(:\r\n    return "é"\r\n'},
             'Show copperLatch', 4000, 'routed', ('def copperLatch(:', 'return "é"'), ()),
            ('contiguous-text-proof', {'record.tmpl':
                'copperLatch\r\n    ivoryRelay = "雪"\r\n    endRecord\r\n'},
             'Show contiguous text copperLatch ivoryRelay', 4000, 'routed',
             ('copperLatch\r\n    ivoryRelay = "雪"\r\n    endRecord',), ()),
        ]

    original_router = harness.ContextRouter

    class CheckedRouter(original_router):
        def route(self, *args, **kwargs):
            result = super().route(*args, **kwargs)
            expected = 'failed' if isinstance(self.selector, harness.UnavailableSelector) else 'not-attempted'
            for trace in (result.metrics, result.metrics['external_selector'], result.metrics['ledger']):
                assert trace['transport_status'] == expected
            assert not result.metrics['ledger']['response_received']
            assert result.metrics['completion']['attempts'] <= 1
            if 'def copperLatch(:' in result.context:
                assert result.metrics['fallback']['syntax']['used']
                assert all('syntax-fallback' in b['precision']
                           for b in result.metrics['rendered_evidence']['blocks'])
            return result

    setattr(harness, 'cases', cases)
    setattr(harness, 'ContextRouter', CheckedRouter)
    harness.run(output)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, required=True)
    run(parser.parse_args().output)
