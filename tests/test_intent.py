from jev_context_router.intent import classify_code_request, is_code_request


def test_code_actions_are_detected():
    assert is_code_request("Fix the failing TypeScript test")
    assert is_code_request("Implémente cette API en Python")
    query = (
        "Debug une anomalie de qualité de données : retrouver dans le dépôt le pipeline "
        "qui collecte des organisations de santé, filtre Marseille, enrichit les numéros "
        "de téléphone, puis exporte CSV et JSON."
    )
    result = classify_code_request(query)
    assert result["intent"] == "code"
    assert result["confidence"] >= 0.9
    assert {"action", "artifact"} <= set(result["matched_signals"])


def test_meta_explanations_do_not_trigger_code_retrieval():
    assert not is_code_request("Pourquoi le routeur code ne se lance pas forcément ?")
    assert not is_code_request("Explique comment fonctionne une classe Python")
