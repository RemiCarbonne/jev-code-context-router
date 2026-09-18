from jev_context_router.intent import is_code_request


def test_code_actions_are_detected():
    assert is_code_request("Fix the failing TypeScript test")
    assert is_code_request("Implémente cette API en Python")


def test_meta_explanations_do_not_trigger_code_retrieval():
    assert not is_code_request("Pourquoi le routeur code ne se lance pas forcément ?")
    assert not is_code_request("Explique comment fonctionne une classe Python")
