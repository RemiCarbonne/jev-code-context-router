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


class ExternalProviderError(RuntimeError):
    """A secret-safe external failure that preserves machine-readable cause data."""

    def __init__(self, provider: str, exc: Exception):
        self.provider = provider
        self.cause_type = type(exc).__name__
        status = getattr(exc, "code", None)
        self.status_code = status if isinstance(status, int) else None
        self.safe_message = f"{provider} request failed: {self.cause_type}"
        if self.status_code is not None:
            self.safe_message += f" status={self.status_code}"
        super().__init__(self.safe_message)


class RoutingStageError(RoutingError):
    """Wrap an unexpected exception with its original traceback."""

    def __init__(self, stage: str, exc: Exception, trace: str):
        root = exc
        while root.__cause__ is not None:
            root = root.__cause__
        self.exception_type = str(getattr(exc, "cause_type", type(root).__name__))
        status = getattr(exc, "status_code", getattr(root, "code", None))
        self.status_code = status if isinstance(status, int) else None
        self.safe_message = str(
            getattr(exc, "safe_message", f"Stage `{stage}` failed: {self.exception_type}")
        )
        super().__init__(stage, self.safe_message)
        self.trace = trace
