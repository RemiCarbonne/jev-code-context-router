import pytest

from jev_context_router.config import Settings
from jev_context_router.router import ContextRouter
from jev_context_router.index import CodeIndex
from jev_context_router.models import Repository, RegionProposal, SourceSpan, Symbol
from jev_context_router.regions import canonicalize
import jev_context_router.router as router_module


def structural_index(tmp_path, raw, proposals, path='engine.unit'):
    regions = canonicalize(path, raw, proposals)
    symbols = [Symbol(r.region_id, path, r.names[0], r.names[0], r.kinds[0],
                      'unspecified', r.span.start_line, r.span.end_line,
                      raw[r.span.start_byte:r.span.end_byte].decode(),
                      region_id=r.region_id, start_byte=r.span.start_byte,
                      end_byte=r.span.end_byte, file_sha256=r.file_sha256,
                      snapshot=raw) for r in regions]
    return CodeIndex(Repository(tmp_path, tmp_path.name), symbols, regions=regions)


def test_complete_unit_promotes_body_to_smallest_declared_parent(tmp_path, monkeypatch):
    raw = b'unit checksumStage(value) {\n  checksumStage(value);\n}\nunrelated outside\n'
    end = raw.index(b'unrelated')
    begin = raw.index(b'  checksum')
    index = structural_index(tmp_path, raw, [
        RegionProposal(SourceSpan(0, end), ('checksumStage',), ('function',), provenance=('test-adapter',)),
        RegionProposal(SourceSpan(begin, end - 2), ('checksumStage',), ('block',), provenance=('test-adapter',)),
        RegionProposal(SourceSpan(0, len(raw)), ('engine',), ('module',), provenance=('test-adapter',)),
    ])
    monkeypatch.setattr(router_module, 'index_repository', lambda *a, **kw: index)
    result = route_fixture(tmp_path, {}, 'Show complete function checksumStage')
    assert raw[:end].decode() in result.context
    assert 'unrelated outside' not in result.context
    assert result.status == 'routed'

def test_sibling_obligations_share_smallest_structural_parent(tmp_path, monkeypatch):
    raw = b'outer {\n  envelope {\n    alphaStamp();\n    omegaStamp();\n  }\n  noise();\n}\n'
    start, end = raw.index(b'  envelope'), raw.index(b'  noise')
    proposals = [RegionProposal(SourceSpan(0, len(raw)), ('outer',), ('module',)),
                 RegionProposal(SourceSpan(start, end), ('envelope',), ('object',))]
    for name in (b'alphaStamp', b'omegaStamp'):
        begin = raw.index(name)
        finish = raw.index(b'\n', begin)
        proposals.append(RegionProposal(SourceSpan(begin, finish), (name.decode(),), ('block',)))
    index = structural_index(tmp_path, raw, proposals)
    monkeypatch.setattr(router_module, 'index_repository', lambda *a, **kw: index)
    result = route_fixture(tmp_path, {}, 'Show alphaStamp and omegaStamp')
    assert raw[start:end].decode() in result.context
    assert 'noise()' not in result.context
    assert len(result.metrics['rendered_evidence']['blocks']) == 1
    assert result.status == 'routed'


def test_complete_request_cannot_be_certified_by_a_packed_prefix(tmp_path):
    source = 'def amberDigest(value):\n' + '    value = value + 1\n' * 30 + '    return value\n'
    result = route_fixture(tmp_path, {'digest.py': source},
                           'Show complete function amberDigest', max_context_chars=200)
    assert 'def amberDigest' in result.context
    assert 'return value' not in result.context
    assert len(result.context) <= 200
    assert result.status == 'insufficient'
    assert result.metrics['coverage']['status'] == 'insufficient'


def test_exclusions_survive_dependency_expansion(tmp_path):
    source = ('class Packet:\n'
              '    def gammaPulse(self):\n        return 7\n'
              '    def examples(self):\n        return self.gammaPulse()\n')
    result = route_fixture(tmp_path, {'packet.py': source},
                           'Show complete function gammaPulse excluding examples')
    assert 'def gammaPulse' in result.context
    assert 'examples' not in result.context
    assert result.status == 'routed'


def test_parent_render_certifies_child_without_redundant_selection(tmp_path):
    from jev_context_router.coverage import build_query_plan, coverage_report, evidence_for_symbols
    from jev_context_router.render import render_context_detailed
    raw = b'frame {\n  alphaStamp();\n}\n'
    begin = raw.index(b'alphaStamp')
    index = structural_index(tmp_path, raw, [
        RegionProposal(SourceSpan(0, len(raw)), ('frame',), ('object',)),
        RegionProposal(SourceSpan(begin, raw.index(b'\n', begin)), ('alphaStamp',), ('block',)),
    ])
    parent, child = index.candidates
    plan = build_query_plan('Show alphaStamp')
    rendered = render_context_detailed(index.repository, [parent], 1000)
    report = coverage_report(plan, index.candidates, rendered, evidence_for_symbols(plan, [child]))
    assert report.status == 'sufficient'


def test_embedded_declaration_keeps_signature_and_nested_object(tmp_path):
    source = ('---\n'
              'import { prismFold } from "prism";\n'
              'function buildPrism(value) {\n'
              '  const payload = { nested: { code: prismFold(value) } };\n'
              '  return payload;\n}\n'
              '---\n<section>outside marker</section>\n')
    result = route_fixture(tmp_path, {'view.astro': source},
                           'Show complete function buildPrism in view.astro')
    assert 'function buildPrism(value) {' in result.context
    assert 'const payload = { nested: { code: prismFold(value) } };' in result.context
    assert '  return payload;\n}' in result.context
    assert '<section>' not in result.context
    assert result.status == 'routed'


def test_conjunctive_obligation_uses_union_when_no_single_region_proves_it(tmp_path):
    result = route_fixture(tmp_path, {
        'first.py': 'def alphaStamp():\n    return 3\n',
        'second.py': 'def omegaStamp():\n    return 5\n',
        'noise.py': 'def spareMarker():\n    return 9\n',
    }, 'alphaStamp omegaStamp')
    assert 'def alphaStamp' in result.context
    assert 'def omegaStamp' in result.context
    assert 'spareMarker' not in result.context
    assert result.status == 'routed'


def test_missing_declaration_parent_does_not_certify_complete_function(tmp_path, monkeypatch):
    raw = b'  deltaStage(value);\n'
    index = structural_index(tmp_path, raw, [
        RegionProposal(SourceSpan(0, len(raw)), ('deltaStage',), ('block',))])
    monkeypatch.setattr(router_module, 'index_repository', lambda *a, **kw: index)
    result = route_fixture(tmp_path, {}, 'Show complete function deltaStage')
    assert 'deltaStage(value)' in result.context  # useful fallback retained
    assert result.status == 'insufficient'


def test_complete_method_does_not_expand_to_unrequested_class(tmp_path):
    source = ('class Carrier:\n'
              '    def amberPulse(self):\n        return 7\n'
              '    def unrelatedMember(self):\n        return 18\n')
    result = route_fixture(tmp_path, {'carrier.py': source}, 'Show complete function amberPulse')
    assert 'def amberPulse' in result.context
    assert 'unrelatedMember' not in result.context
    assert result.status == 'routed'


def test_common_parent_covers_all_branches_not_only_first_pair(tmp_path, monkeypatch):
    raw = b'container {\n  pair { alphaStamp(); omegaStamp(); }\n  gammaStamp();\n}\noutside\n'
    end = raw.index(b'outside')
    pair_start, pair_end = raw.index(b'  pair'), raw.index(b'\n  gamma')
    proposals = [RegionProposal(SourceSpan(0, len(raw)), ('file',), ('module',)),
                 RegionProposal(SourceSpan(0, end), ('container',), ('object',)),
                 RegionProposal(SourceSpan(pair_start, pair_end), ('pair',), ('object',))]
    for name in (b'alphaStamp', b'omegaStamp', b'gammaStamp'):
        begin = raw.index(name)
        proposals.append(RegionProposal(SourceSpan(begin, begin + len(name) + 3), (name.decode(),), ('block',)))
    index = structural_index(tmp_path, raw, proposals)
    monkeypatch.setattr(router_module, 'index_repository', lambda *a, **kw: index)
    result = route_fixture(tmp_path, {}, 'Show alphaStamp and omegaStamp and gammaStamp')
    assert raw[:end].decode() in result.context
    assert 'outside' not in result.context
    assert result.status == 'routed'


def test_oversized_parent_keeps_compact_child_union(tmp_path, monkeypatch):
    raw = b'container {\n  alphaStamp();\n' + b'  padding();\n' * 100 + b'  omegaStamp();\n}\n'
    proposals = [RegionProposal(SourceSpan(0, len(raw)), ('container',), ('object',))]
    for name in (b'alphaStamp', b'omegaStamp'):
        begin = raw.index(name)
        proposals.append(RegionProposal(SourceSpan(begin, begin + len(name) + 3), (name.decode(),), ('block',)))
    index = structural_index(tmp_path, raw, proposals)
    monkeypatch.setattr(router_module, 'index_repository', lambda *a, **kw: index)
    result = route_fixture(tmp_path, {}, 'Show alphaStamp and omegaStamp', max_context_chars=220)
    assert 'alphaStamp();' in result.context
    assert 'omegaStamp();' in result.context
    assert 'padding' not in result.context
    assert len(result.context) <= 220
    assert result.status == 'routed'


def test_complete_function_resolution_does_not_drop_separate_import(tmp_path):
    source = ('from prism import prismFold\n'
              'def buildPrism(value):\n    return prismFold(value)\n')
    result = route_fixture(tmp_path, {'prism.py': source},
                           'Show import prismFold and complete function buildPrism in prism.py')
    assert 'from prism import prismFold' in result.context
    assert 'def buildPrism(value):' in result.context
    assert 'return prismFold(value)' in result.context
    assert result.status == 'routed'


def test_single_file_anchor_applies_to_coordinated_targets(tmp_path):
    source = ('import { AmberLink } from "amber-transport";\n'
              'function connectRelay() {\n  return deltaChecksum();\n}\n')
    result = route_fixture(tmp_path, {'relay.ts': source, 'elsewhere.ts': 'import AmberLink;\n'},
                           'Show import AmberLink and callback deltaChecksum in relay.ts')
    assert 'return deltaChecksum()' in result.context
    assert result.metrics['included_files'] == ['relay.ts']
    assert result.status == 'routed'


def test_complete_sibling_functions_are_certified_inside_parent(tmp_path):
    source = ('class Carrier:\n'
              '    def alphaStamp(self):\n        return 7\n'
              '    def omegaStamp(self):\n        return 18\n')
    result = route_fixture(tmp_path, {'carrier.py': source},
                           'Show complete function alphaStamp and complete function omegaStamp')
    assert 'def alphaStamp' in result.context and 'def omegaStamp' in result.context
    assert result.status == 'routed'


@pytest.mark.parametrize('excluded', ['examples', 'previews', 'stories', 'notes', 'comments'])
def test_excluded_parent_leaves_only_safe_child_union(tmp_path, monkeypatch, excluded):
    raw = ('container {\n  alphaStamp();\n  ' + excluded + ';\n  omegaStamp();\n}\n').encode()
    proposals = [RegionProposal(SourceSpan(0, len(raw)), ('container',), ('object',))]
    for name in (b'alphaStamp', b'omegaStamp'):
        begin = raw.index(name)
        proposals.append(RegionProposal(SourceSpan(begin, begin + len(name) + 3), (name.decode(),), ('block',)))
    index = structural_index(tmp_path, raw, proposals)
    monkeypatch.setattr(router_module, 'index_repository', lambda *a, **kw: index)
    result = route_fixture(tmp_path, {}, 'Show alphaStamp and omegaStamp excluding ' + excluded)
    assert 'alphaStamp();' in result.context and 'omegaStamp();' in result.context
    assert excluded not in result.context
    assert result.status == 'routed'


def test_module_parent_is_not_whole_file_default(tmp_path, monkeypatch):
    raw = b'alphaStamp();\nunrequested payload\nomegaStamp();\n'
    proposals = [RegionProposal(SourceSpan(0, len(raw)), ('file',), ('module',))]
    for name in (b'alphaStamp', b'omegaStamp'):
        begin = raw.index(name)
        proposals.append(RegionProposal(SourceSpan(begin, begin + len(name) + 3), (name.decode(),), ('block',)))
    index = structural_index(tmp_path, raw, proposals)
    monkeypatch.setattr(router_module, 'index_repository', lambda *a, **kw: index)
    result = route_fixture(tmp_path, {}, 'Show alphaStamp and omegaStamp')
    assert 'unrequested payload' not in result.context
    assert result.status == 'routed'


def test_embedded_parent_cache_and_utf8_coordinates(tmp_path):
    from jev_context_router.index import index_repository
    raw = ('---\r\nconst label = "雪";\r\nfunction prismStage(value) {\r\n'
           '  const result = { token: "é", nested: { value } };\r\n'
           '  return result;\r\n}\r\n---\r\n<aside>outside</aside>\r\n').encode()
    (tmp_path / 'view.astro').write_bytes(raw)
    repo = Repository(tmp_path, 'local')
    cold = index_repository(repo, cache_path=tmp_path / 'cache.json')
    warm = index_repository(repo, cache_path=tmp_path / 'cache.json')
    assert cold.regions == warm.regions
    assert [(s.id, s.source) for s in cold.candidates] == [(s.id, s.source) for s in warm.candidates]
    declaration = next(s for s in warm.candidates if s.name == 'prismStage')
    assert raw[declaration.start_byte:declaration.end_byte].decode() == declaration.source
    assert 'return result;\r\n}' in declaration.source
    assert '<aside>' not in declaration.source


def route_fixture(tmp_path, sources, query, **overrides):
    (tmp_path / '.git').mkdir(exist_ok=True)
    for path, source in sources.items():
        target = tmp_path / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(source)
    settings = Settings(workspace_roots=(tmp_path,), lexical_enabled=False,
                        index_cache_enabled=False, **overrides)
    return ContextRouter(settings).route(query, cwd=tmp_path, force=True)


def test_import_and_callback_are_independent_obligations(tmp_path):
    source = ('import { AmberLink } from "amber-transport";\n'
              'export function connectRelay() {\n'
              '  return attachSocket(value => {\n'
              '    return deltaChecksum(value);\n'
              '  });\n}\n'
              'export function unrelatedMarker() { return 19; }\n')
    result = route_fixture(tmp_path, {'relay.ts': source},
                           'Show import AmberLink and callback deltaChecksum in relay.ts')
    assert 'import { AmberLink }' in result.context
    assert 'return deltaChecksum(value);' in result.context
    assert 'unrelatedMarker' not in result.context
    assert result.metrics['requirements_planned'] == 2
    assert result.status == 'routed'
