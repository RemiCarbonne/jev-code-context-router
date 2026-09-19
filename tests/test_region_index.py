from pathlib import Path

import pytest

from jev_context_router.index import index_repository
from jev_context_router.models import Repository
from jev_context_router.ranking import shortlist
from jev_context_router.regions import source_text


@pytest.mark.parametrize('suffix,source', [
    ('.py', '@decorator\r\nclass Café:\r\n    def run(self):\r\n        return "雪"\r\n'),
    ('.astro', '<Layout canonical={url} layout={x}><a href={url}>雪</a></Layout>\r\n'),
    ('.vue', '<template><p>雪</p></template>\r\n<script>const a=1;</script>\r\n'),
    ('.svelte', '<script>let a=1;</script>\n<p>{a}</p>\n'),
    ('.html', '<main>雪</main>\n'),
    ('.flux', 'snow 雪\r\nsecond\r\n'),
    ('.py', 'def broken(:\n    snow = "雪"\n'),
])
def test_adapters_produce_exact_regions(tmp_path, suffix, source):
    path = tmp_path / ('sample' + suffix)
    path.write_bytes(source.encode())
    index = index_repository(Repository(tmp_path, 'demo'), include_paths=[path])
    assert index.regions
    for region in index.regions:
        assert source_text(region, path.read_bytes()) == path.read_bytes()[region.span.start_byte:region.span.end_byte].decode()
    for symbol in index.symbols:
        region = index.regions_by_id[symbol.region_id]
        assert symbol.source == source_text(region, path.read_bytes())
    candidates = shortlist('snow sample Café run canonical url', index, 99)
    assert len({s.region_id for s in candidates}) == len(candidates)
    if suffix == '.py' and source.startswith('@'):
        parent = next(r for r in index.regions if 'Café' in r.names)
        child = next(r for r in index.regions if 'run' in r.names)
        assert parent.span.start_byte == 0
        assert child.parent_id == parent.region_id


def test_generic_remainders_are_contiguous(tmp_path):
    raw = b'const prefix=1;\nfunction run() { return 3; }\nconst suffix=2;\n'
    (tmp_path / 'sample.js').write_bytes(raw)
    index = index_repository(Repository(tmp_path, 'demo'))
    modules = [s for s in index.symbols if s.kind == 'module']
    assert len(modules) == 2
    for symbol in modules:
        assert symbol.source.encode() == raw[symbol.start_byte:symbol.end_byte]


def test_admission_binary_cache_and_old_schema(tmp_path):
    import json
    (tmp_path / 'sample.flux').write_text('snow\n')
    (tmp_path / 'bad.flux').write_bytes(b'abc\x00def')
    repo = Repository(tmp_path, 'demo')
    assert not index_repository(repo).symbols
    cache = tmp_path / 'cache.json'
    cache.write_text(json.dumps({'version': 3, 'files': {'sample.flux': {'symbols': []}}}))
    cold = index_repository(repo, admitted_globs=('*.flux',), cache_path=cache)
    warm = index_repository(repo, admitted_globs=('*.flux',), cache_path=cache)
    assert cold.regions == warm.regions
    assert cold.symbols == warm.symbols
    assert warm.stats['index_cache_hit']
    assert {s.path for s in warm.symbols} == {'sample.flux'}
    assert not index_repository(repo, cache_path=cache).symbols
