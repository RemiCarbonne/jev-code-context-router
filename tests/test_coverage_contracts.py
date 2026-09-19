from pathlib import Path

from jev_context_router.coverage import (
    build_query_plan, can_finish_locally, coverage_report, evidence_for_symbols,
)
from jev_context_router.index import index_repository
from jev_context_router.models import Repository
from jev_context_router.security import PathPolicy


def test_cross_cutting_plan_keeps_four_independent_obligations_in_both_languages():
    for query in (
        "Trace the URL policy across middleware, layout, sitemap, and navigation",
        "Tracer la politique d'URL à travers middleware, layout, sitemap et navigation",
    ):
        plan = build_query_plan(query)
        assert plan.scope == "cross-cutting"
        assert len(plan.requirements) == 4
        assert not can_finish_locally(plan, ())


def test_astro_without_exports_has_selectable_regions(tmp_path: Path):
    (tmp_path / "page.astro").write_text(
        "---\nconst canonical = Astro.url.href;\nconst links = [{href: '/one'}];\n---\n"
        "<Layout canonical={canonical}><a href=\"/two\">Two</a></Layout>\n"
        "<style>body { color: red }</style>\n", encoding="utf-8"
    )
    repo = Repository(tmp_path, "synthetic")
    indexed = index_repository(repo, PathPolicy(), max_files=10)
    assert {symbol.kind for symbol in indexed.symbols} >= {"module", "template"}
    assert any("canonical" in symbol.source for symbol in indexed.symbols)
    warm = index_repository(repo, PathPolicy(), max_files=10)
    assert [(s.id, s.source) for s in indexed.symbols] == [(s.id, s.source) for s in warm.symbols]


def test_rendered_context_is_the_authority_for_coverage():
    plan = build_query_plan("Trace middleware across layout and navigation")
    class S:
        pass
    from jev_context_router.models import Symbol
    symbols = [Symbol("a", "a.ts", "middleware", "middleware", "module", "typescript", 1, 2, "middleware")]
    evidence = evidence_for_symbols(plan, symbols)
    report = coverage_report(plan, symbols, ("not-rendered",), evidence)
    assert report.status == "insufficient"
    assert report.missing
    assert report.reasons[report.missing_requirements[0]] in {"no-local-proof", "selected-but-not-rendered"}


def test_composite_mandatory_terms_are_conjunctive_and_anchor_is_not_proof():
    from jev_context_router.models import CandidateEvidence, CoverageRequirement, QueryPlan, Symbol
    plan = QueryPlan("normalization policy exception handling", "localized", (
        CoverageRequirement("r", "normalization policy exception handling", expected_terms=("normalization", "policy", "exception", "handling"),
                            mandatory_terms=("normalization", "policy", "exception", "handling"), anchors=("config.py",)),
    ))
    symbol = Symbol("config.py::rule", "config.py", "rule", "rule", "function", "python", 1, 1,
                    "normalization policy exception")
    evidence = [CandidateEvidence("config.py::rule", "config.py", (1, 1), ("r",), symbol.source)]
    assert coverage_report(plan, [symbol], (symbol.id,), evidence).status == "insufficient"
    assert coverage_report(plan, [symbol], (symbol.id,), evidence).missing == 1


def test_astro_specialized_regions_are_atomic_and_have_no_punctuation_residue(tmp_path: Path):
    source = """---\nconst canonical = makeCanonical(base, {locale: lang});\n---\n<Layout canonical={canonical} layout={{value: canonical, nested: {x: 1}}}>\n  <a\n    href={nav[0].href}\n  >link</a>\n</Layout>\n"""
    (tmp_path / "adversarial.astro").write_text(source, encoding="utf-8")
    indexed = index_repository(Repository(tmp_path, "synthetic"), PathPolicy(), max_files=10)
    specialized = [s for s in indexed.symbols if s.name in {"canonical", "layout", "href"}]
    assert {s.name for s in specialized} >= {"canonical", "layout", "href"}
    assert all(s.source.strip(" <>\n\t;") for s in specialized)
    assert all(s.start_line <= s.end_line for s in specialized)


def test_empty_or_truncated_proof_never_creates_coverage():
    from jev_context_router.models import CandidateEvidence, CoverageRequirement, QueryPlan, Symbol
    plan = QueryPlan("normalization policy exception handling", "localized", (
        CoverageRequirement("r", "normalization policy exception handling",
                            expected_terms=("normalization", "policy", "exception", "handling"),
                            mandatory_terms=("normalization", "policy", "exception", "handling")),
    ))
    symbol = Symbol("r", "rules.py", "rule", "rule", "function", "python", 1, 1, "handling")
    for excerpt in ("", "normalization policy exception"):
        evidence = [CandidateEvidence("r", "rules.py", (1, 1), ("r",), excerpt)]
        assert coverage_report(plan, [symbol], ("r",), evidence).status == "insufficient"
