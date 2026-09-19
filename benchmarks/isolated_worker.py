"""Oracle-blind worker. Run only in the orchestrator's isolated container."""
import json
import os
from pathlib import Path
import socket
import sys
from unittest.mock import patch

from jev_context_router.config import Settings
from jev_context_router.index import index_repository
from jev_context_router.models import Repository
from jev_context_router.providers import LocalSelector
from jev_context_router.router import ContextRouter
from jev_context_router.security import PathPolicy
from selector_stubs import OfflineSelector


def main():
    for key in list(os.environ):
        if key == 'TYPESAFE_API_KEY' or key.startswith('JEV_CONTEXT_'):
            del os.environ[key]
    assert not Path('/eval/oracle.json').exists()
    assert not Path('/oracle.json').exists()
    assert not Path('/var/run/docker.sock').exists()
    try:
        with socket.socket() as sock:
            sock.settimeout(0.2)
            sock.connect(('192.0.2.1', 443))
    except OSError as exc:
        denial = type(exc).__name__
    else:
        raise RuntimeError('sandbox failed to deny network')
    root = Path('/corpus')
    inputs = json.loads(Path('/inputs.json').read_text())
    rows = []
    for number, case in enumerate(inputs['cases']):
        allowed = frozenset(case['admissible_files'])

        class AdmissionPolicy(PathPolicy):
            def allows(self, root, path):
                return super().allows(root, path) and path.resolve().relative_to(root).as_posix() in allowed

        def admitted_index(repository, policy=None, **kwargs):
            # Admission comes only from public inputs, including distractors.
            # No ordering, annotations or semantic labels are injected.
            return index_repository(repository, AdmissionPolicy(), admitted_globs=('*',), **kwargs)

        for repeat in range(1, 4):
            for cache_mode in ('cold', 'warm'):
                cache = Path('/run-results/cache') / str(number) / str(repeat)
                if cache_mode == 'cold':
                    assert not cache.exists()
                settings = Settings(workspace_roots=(root,), lexical_enabled=False,
                    index_cache_dir=cache, max_context_chars=case['budget']['max_context_chars'],
                    selector_max_input_bytes=case['budget']['selector_max_input_bytes'])
                selector = LocalSelector() if case['selector_mode'] == 'local' else OfflineSelector(settings, case['selector_mode'])
                # The corpus intentionally has no project marker. Repository
                # resolution is explicit, not part of this evidence evaluation.
                repository = Repository(root, 'corpus')
                with (patch('jev_context_router.router.discover_repositories', return_value=[repository]),
                      patch('jev_context_router.router.resolve_repository', return_value=(repository, 'evaluation-explicit')),
                      patch('jev_context_router.router.index_repository', admitted_index)):
                    result = ContextRouter(settings, selector).route(case['query'], cwd=root, force=True)
                evidence = result.metrics.get('rendered_evidence', {})
                trace = dict(result.metrics['ledger'])
                trace['network_calls'] = 0
                trace['transport_calls'] = getattr(selector, 'transport_calls', 0)
                rows.append({'scenario_id': case['id'], 'cache_mode': cache_mode, 'repeat': repeat,
                    'status': result.metrics.get('coverage', {}).get('status', 'insufficient'),
                    'context': result.context, 'blocks': evidence.get('blocks', []),
                    'metadata': evidence.get('metadata', []), 'trace': trace,
                    'captured_payload_octets': list(getattr(selector, 'captured', b'')),
                    'router_status': result.status, 'router_metrics': result.metrics})
        print(json.dumps({'completed_cases': number + 1, 'runs': len(rows)}), flush=True)
    Path('/run-results/benchmark.json').write_text(json.dumps({'runs': rows}, ensure_ascii=False))
    Path('/run-results/isolation.json').write_text(json.dumps({'network_denial': denial,
        'oracle_visible': False, 'docker_socket_visible': False,
        'corpus_files': sum(p.is_file() for p in root.rglob('*'))}))


if __name__ == '__main__':
    main()
