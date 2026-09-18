from __future__ import annotations

import re

_CODE_INTENT = re.compile(
    r"\b(code|coding|coder|bug|fix|correctif|patch|fonction|function|classe|class|"
    r"méthode|method|test|tests|pytest|unittest|compiler|compile|compilation|"
    r"implément|implement|refactor|repository|service|api|endpoint|stack trace|"
    r"exception|typescript|javascript|python|golang|rust|java|php|fichier source|"
    r"source file|module|migration|schema|database|sql|frontend|backend)\b",
    re.I,
)
_NON_ACTION_EXPLANATION = re.compile(
    r"^\s*(pourquoi|why|explique|explain|qu['’]est[- ]ce|what is|comment fonctionne|how does)\b",
    re.I,
)
_ACTION = re.compile(
    r"\b(corrige|fix|change|modifie|modify|ajoute|add|crée|create|implémente|implement|"
    r"refactor|debug|répare|repair|écris|write|build|teste|test|review|audit)\b",
    re.I,
)


def is_code_request(query: str) -> bool:
    if not _CODE_INTENT.search(query):
        return False
    if _NON_ACTION_EXPLANATION.search(query) and not _ACTION.search(query):
        return False
    return True
