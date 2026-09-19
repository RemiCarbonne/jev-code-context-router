from __future__ import annotations

import re

_SIGNALS: dict[str, re.Pattern[str]] = {
    "action": re.compile(
        r"\b(debug|débogu(?:e|er)|corrig(?:e|er)|fix|patch|modifi(?:e|er)|change|"
        r"ajout(?:e|er)|cré(?:e|er)|implément(?:e|er)|implement|refactori(?:se|ser)|"
        r"refactor|répar(?:e|er)|repair|écri(?:s|re)|write|build|test(?:e|er)?|review|"
        r"audit|analys(?:e|er)|retrouv(?:e|er)|identifi(?:e|er)|trac(?:e|er)|"
        r"expliqu(?:e|er) le bug|diagnostiqu(?:e|er))\b",
        re.I,
    ),
    "artifact": re.compile(
        r"\b(code|coding|bug|correctif|fonction|functions?|classe|class|méthode|methods?|"
        r"tests?|pytest|unittest|compilation|implémentation|implementation|repository|"
        r"dépôt|fichiers?|files?|symbols?|symboles?|pipeline|modules?|service|api|endpoint|"
        r"stack trace|exception|typescript|javascript|python|golang|rust|java|php|"
        r"source|migration|schema|database|sql|frontend|backend|export|validation|"
        r"transaction)\b",
        re.I,
    ),
}
_NON_ACTION_EXPLANATION = re.compile(
    r"^\s*(pourquoi|why|explique|explain|qu['’]est[- ]ce|what is|comment fonctionne|how does)\b",
    re.I,
)
_PATH_OR_IDENTIFIER = re.compile(r"(?:[\w.-]+/)+[\w.-]+|\b[A-Za-z_$][A-Za-z0-9_$]{5,}\b")


def classify_code_request(query: str) -> dict:
    """Classify coding intent and expose stable, non-model diagnostics."""
    matched = [name for name, pattern in _SIGNALS.items() if pattern.search(query)]
    if _PATH_OR_IDENTIFIER.search(query):
        matched.append("code-reference")
    explanatory = bool(_NON_ACTION_EXPLANATION.search(query))
    action = "action" in matched
    artifact = "artifact" in matched
    is_code = action and (artifact or "code-reference" in matched)
    if explanatory and not action:
        is_code = False
    confidence = 0.95 if action and artifact else 0.78 if is_code else 0.15 if matched else 0.02
    return {
        "intent": "code" if is_code else "not-code",
        "confidence": confidence,
        "matched_signals": matched,
        "reason": "action-and-code-signal" if is_code else "insufficient-code-action-signals",
    }


def is_code_request(query: str) -> bool:
    return classify_code_request(query)["intent"] == "code"


def analyze_query_scope(query: str) -> str:
    """Analyze retrieval scope independently from the code-intent gate."""
    from .coverage import analyze_query_scope as _analyze
    return _analyze(query)


def build_query_plan(query: str):
    from .coverage import build_query_plan as _build
    return _build(query)
