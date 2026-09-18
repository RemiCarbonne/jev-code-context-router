from __future__ import annotations


class RoutingError(RuntimeError):
    """Base class for structured routing failures."""

    def __init__(self, stage: str, message: str):
        super().__init__(message)
        self.stage = stage
        self.message = message


class RoutingTimeout(RoutingError):
    """Raised when a bounded routing stage exceeds its deadline."""


class RoutingLimitExceeded(RoutingError):
    """Raised when a configured repository safety budget is exhausted."""


class RoutingStageError(RoutingError):
    """Wrap an unexpected exception with its original traceback."""

    def __init__(self, stage: str, exc: Exception, trace: str):
        super().__init__(stage, str(exc) or type(exc).__name__)
        self.exception_type = type(exc).__name__
        self.trace = trace
