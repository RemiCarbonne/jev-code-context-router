"""Independent-of-external-data local regression benchmark; no provider/network.

Builds only the fixtures below in a temporary workspace. This does not import or
run the separate evaluation harness. Output records real rendered-byte checks.
"""
from __future__ import annotations

import argparse
from collections import defaultdict
import hashlib
import json
from pathlib import Path
import socket
import tempfile

from jev_context_router.config import Settings
from jev_context_router.providers import LocalSelector
from jev_context_router.router import ContextRouter


class UnavailableSelector(LocalSelector):
    def select_symbols(self, *args, **kwargs):
        raise OSError('local deterministic unavailable selector')


def deny_network(*args, **kwargs):
    raise AssertionError('network disabled in local benchmark')


def cases():
    return [
        ('import-callback', {'relay.ts':
            'import { AmberLink } from "amber-transport";\n'
            'export function connectRelay() {\n  return attachSocket(value => {\n'
            '    return deltaChecksum(value);\n  });\n}\n'
            'export function unrelatedMarker() { return 19; }\n'},
         'Show import AmberLink and callback deltaChecksum in relay.ts', 4000, 'routed',
         ('import { AmberLink }', 'return deltaChecksum(value);'), ('unrelatedMarker',)),
        ('embedded-complete', {'view.astro':
            '---\nimport { prismFold } from "prism";\nfunction buildPrism(value) {\n'
            '  const payload = { nested: { code: prismFold(value) } };\n'
            '  return payload;\n}\n---\n<section>outside marker</section>\n'},
         'Show complete function buildPrism in view.astro', 4000, 'routed',
         ('function buildPrism(value) {', 'const payload =', '  return payload;\n}'), ('<section>',)),
        ('complete-method', {'carrier.py':
            'class Carrier:\n    def amberPulse(self):\n        return 7\n'
            '    def unrelatedMember(self):\n        return 18\n'},
         'Show complete function amberPulse', 4000, 'routed',
         ('def amberPulse(self):', 'return 7'), ('unrelatedMember',)),
        ('budget-prefix', {'digest.py':
            'def amberDigest(value):\n' + '    value = value + 1\n' * 30 + '    return value\n'},
         'Show complete function amberDigest', 200, 'insufficient',
         ('def amberDigest',), ('return value',)),
        ('multi-file-union', {'first.py': 'def alphaStamp():\n    return 3\n',
                              'second.py': 'def omegaStamp():\n    return 5\n',
                              'noise.py': 'def spareMarker():\n    return 9\n'},
         'alphaStamp omegaStamp', 4000, 'routed',
         ('def alphaStamp', 'def omegaStamp'), ('spareMarker',)),
        ('excluded-dependency', {'packet.py':
            'class Packet:\n    def gammaPulse(self):\n        return 7\n'
            '    def examples(self):\n        return self.gammaPulse()\n'},
         'Show complete function gammaPulse excluding examples', 4000, 'routed',
         ('def gammaPulse', 'return 7'), ('examples',)),
        ('exhaustive-functions', {f'part{i}.py': f'def amberPulse():\n    return {i}\n' for i in range(3)},
         'Show all complete functions amberPulse', 4000, 'routed',
         ('return 0', 'return 1', 'return 2'), ()),
        ('complete-siblings', {'carrier.py':
            'class Carrier:\n    def alphaStamp(self):\n        return 7\n'
            '    def omegaStamp(self):\n        return 18\n'},
         'Show complete function alphaStamp and complete function omegaStamp', 4000, 'routed',
         ('def alphaStamp', 'def omegaStamp'), ()),
        ('template-parent', {'envelope.astro':
            '<main>\n<small>{alphaStamp()}</small>\n<strong>{omegaStamp()}</strong>\n</main>\n'
            '<aside>unrelatedMarker</aside>\n'},
         'Show alphaStamp and omegaStamp in envelope.astro', 4000, 'routed',
         ('<main>', 'alphaStamp()', 'omegaStamp()', '</main>'), ('unrelatedMarker',)),
    ]


def run(output):
    socket.socket.connect = deny_network
    socket.socket.connect_ex = deny_network
    socket.create_connection = deny_network
    socket.getaddrinfo = deny_network
    rows = []
    fingerprints = defaultdict(set)
    definitions = cases()
    with tempfile.TemporaryDirectory(prefix='structural-local-') as temporary:
        root = Path(temporary)
        for label, sources, query, budget, status, required, forbidden in definitions:
            repo = root / label
            (repo / '.git').mkdir(parents=True)
            for path, source in sources.items():
                (repo / path).write_bytes(source.encode())
            for mode in ('local', 'unavailable'):
                for repeat in range(3):
                    cache = root / 'caches' / label / mode / str(repeat)
                    settings = Settings(workspace_roots=(root,), lexical_enabled=False,
                                        index_cache_enabled=True, index_cache_dir=cache,
                                        metrics_path=None, max_context_chars=budget)
                    selector = LocalSelector() if mode == 'local' else UnavailableSelector()
                    router = ContextRouter(settings, selector)
                    for temperature in ('cold', 'warm'):
                        result = router.route(query, cwd=repo, force=True)
                        failures = []
                        if result.status != status:
                            failures.append(f'status: {result.status} != {status}')
                        failures.extend(f'missing: {text}' for text in required if text not in result.context)
                        failures.extend(f'forbidden: {text}' for text in forbidden if text in result.context)
                        if len(result.context) > budget:
                            failures.append('context budget exceeded')
                        if result.metrics.get('selector_input_tokens') or result.metrics.get('selector_output_tokens'):
                            failures.append('unexpected billed tokens')
                        blocks = result.metrics.get('rendered_evidence', {}).get('blocks', [])
                        for block in blocks:
                            emitted = result.context[block['context_start']:block['context_end']].encode()
                            raw = sources[block['path']].encode()
                            if emitted != raw[block['start_byte']:block['end_byte']]:
                                failures.append('rendered byte mismatch')
                            if hashlib.sha256(raw).hexdigest() != block['file_sha256']:
                                failures.append('snapshot mismatch')
                        fingerprint = hashlib.sha256(json.dumps({
                            'context': result.context, 'status': result.status,
                            'blocks': blocks, 'coverage': result.metrics.get('coverage', {}).get('status'),
                        }, sort_keys=True).encode()).hexdigest()
                        fingerprints[label].add(fingerprint)
                        rows.append({'case': label, 'mode': mode, 'repeat': repeat,
                                     'temperature': temperature, 'status': result.status,
                                     'chars': len(result.context), 'budget': budget,
                                     'blocks': blocks, 'fingerprint': fingerprint,
                                     'cache_hit': result.metrics.get('index_cache_hit'),
                                     'seconds': result.metrics.get('seconds'), 'failures': failures})
    unstable = [label for label, values in fingerprints.items() if len(values) != 1]
    expected = len(definitions) * 2 * 3 * 2
    assert len(rows) == expected
    summary = {'runs': len(rows), 'cases': len(definitions),
               'valid_runs': sum(not row['failures'] for row in rows),
               'unstable_cases': unstable,
               'cold_cache_hits': sum(bool(row['cache_hit']) for row in rows if row['temperature'] == 'cold'),
               'warm_cache_hits': sum(bool(row['cache_hit']) for row in rows if row['temperature'] == 'warm'),
               'network': 'socket operations denied', 'selector_modes': ['local', 'unavailable']}
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps({'summary': summary, 'runs': rows}, indent=2) + '\n')
    print(json.dumps(summary, indent=2))
    assert not unstable and summary['valid_runs'] == expected
    assert summary['cold_cache_hits'] == 0 and summary['warm_cache_hits'] == expected // 2


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, required=True)
    run(parser.parse_args().output)
