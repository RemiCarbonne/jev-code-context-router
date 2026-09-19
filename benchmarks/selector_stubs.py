"""Payload-only deterministic transports. No implementation delegates to HTTP."""
from jev_context_router.providers import JevSelector, serialize_request


class OfflineSelector(JevSelector):
    def __init__(self, settings, mode):
        super().__init__('', settings)
        self.mode = mode
        self.captured = b''
        self.transport_calls = 0

    def _evaluate(self, state, questions):
        self.transport_calls += 1
        if self.mode == 'unavailable':
            raise OSError('synthetic-transport-unavailable')
        self.captured = serialize_request(state, questions, self.settings)
        self.last_payload_bytes = len(self.captured)
        keys = list(questions)
        if self.mode == 'partial':
            keys = keys[:1]
        if self.mode == 'reverse-order':
            keys.reverse()
        scores = {}
        for rank, key in enumerate(keys):
            score = 0 if self.mode == 'all-low' else 2 if self.mode == 'invalid' else 1
            if self.mode == 'reverse-order':
                score = 1 - rank / max(1, len(keys))
            scores[key] = {'noul': score}
        return scores, {'input_tokens': 0, 'output_tokens': 0}, 0.0
