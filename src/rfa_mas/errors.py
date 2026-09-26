from __future__ import annotations


class RfaError(Exception):
    def __init__(self, code: str, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.safe_message = message
        self.retryable = retryable


class ConfigurationError(RfaError):
    def __init__(self, missing: list[str] | tuple[str, ...]) -> None:
        names = ", ".join(sorted(missing))
        super().__init__(
            "configuration_error",
            f"선택한 모드에 필요한 설정이 없습니다: {names}",
        )
        self.missing = tuple(sorted(missing))


class BackendNotImplementedError(RfaError):
    def __init__(self, backend: str) -> None:
        super().__init__(
            "not_implemented",
            f"선택한 backend는 P0에서 구현되지 않았습니다: {backend}",
        )


class PolicyDeniedError(RfaError):
    def __init__(self, message: str = "정책에 따라 요청이 거절되었습니다.") -> None:
        super().__init__("policy_denied", message)


class InvalidStateTransitionError(RfaError):
    def __init__(self, current: str, requested: str) -> None:
        super().__init__(
            "invalid_state_transition",
            f"허용되지 않은 상태 전이입니다: {current} -> {requested}",
        )


class ResourceNotFoundError(RfaError):
    def __init__(self, resource: str) -> None:
        super().__init__("not_found", f"리소스를 찾을 수 없습니다: {resource}")


class BudgetExceededError(RfaError):
    def __init__(self, budget: str) -> None:
        super().__init__("budget_exceeded", f"실행 예산을 초과했습니다: {budget}")


class OutcomeUnknownError(RfaError):
    def __init__(self, operation: str) -> None:
        super().__init__(
            "outcome_unknown",
            f"timeout으로 결과를 확정할 수 없습니다: {operation}",
            retryable=False,
        )


class PublicationNotAttemptedError(RfaError):
    """A publisher refused BEFORE dispatching anything, so no effect can exist (P1-008E).

    Keeps the cause's code (e.g. approval_required) and safe message. Only a publisher that
    has provably sent nothing may raise it; after dispatch a definite refusal is an ordinary
    RfaError (failed) and an unknown result is OutcomeUnknownError.
    """

    def __init__(self, cause: RfaError) -> None:
        super().__init__(cause.code, cause.safe_message, retryable=cause.retryable)
