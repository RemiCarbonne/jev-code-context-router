"""Offline orchestrator: no router or oracle imports in this process."""
import argparse
import os
from pathlib import Path
import subprocess
import sys


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--corpus', type=Path, required=True, help='frozen evaluation package root')
    parser.add_argument('--expected-manifest-sha256', required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--repeat', type=int, choices=[3], default=3)
    parser.add_argument('--offline', action='store_true', required=True)
    parser.add_argument('--mount-prefix', help='Docker daemon path mapping LOCAL=HOST, when nested in a container')
    args = parser.parse_args()

    def host(path):
        if args.mount_prefix:
            local, remote = args.mount_prefix.split('=', 1)
            return Path(remote) / Path(path).relative_to(local)
        return path
    for key in list(os.environ):
        if key == 'TYPESAFE_API_KEY' or key.startswith('JEV_CONTEXT_'):
            del os.environ[key]
    root = args.corpus.resolve()
    out = args.output.resolve()
    if out.exists() or (out.parent / 'cache').exists():
        raise SystemExit('output and cold caches must be fresh')
    out.parent.mkdir(parents=True, exist_ok=True)
    verify = [sys.executable, '-B', str(root / 'verify_results.py'), '--expected-manifest-sha256', args.expected_manifest_sha256]
    subprocess.run(verify, check=True)
    repo = Path(__file__).resolve().parents[1]
    command = ['docker', 'run', '--rm', '--pull', 'never', '--network', 'none', '--read-only',
        '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges', '--user', f'{os.getuid()}:{os.getgid()}',
        '--tmpfs', '/tmp:rw,noexec,nosuid,size=64m', '--pids-limit', '128',
        '-e', 'PYTHONDONTWRITEBYTECODE=1', '-e', 'PYTHONPATH=/src', '-e', 'HOME=/tmp',
        '--mount', f'type=bind,src={host(repo / "src")},dst=/src,readonly',
        '--mount', f'type=bind,src={host(repo / "benchmarks")},dst=/runner,readonly',
        '--mount', f'type=bind,src={host(root / "corpus")},dst=/corpus,readonly',
        '--mount', f'type=bind,src={host(root / "inputs.json")},dst=/inputs.json,readonly',
        '--mount', f'type=bind,src={host(out.parent)},dst=/run-results',
        'python:3.13-slim', 'python', '-B', '/runner/isolated_worker.py']
    subprocess.run(command, check=True)
    produced = out.parent / 'benchmark.json'
    if produced != out:
        produced.rename(out)
    out.chmod(0o444)
    (out.parent / 'isolation.json').chmod(0o444)
    # Router and selector have exited before this separate evaluator reads labels.
    subprocess.run(verify, check=True)
    subprocess.run(verify + ['--results', str(out)], check=True)


if __name__ == '__main__':
    main()
