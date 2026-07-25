from dataclasses import dataclass
from enum import Enum


class RegistrationOutcome(str, Enum):
    """Stable registration outcome labels for scheduler and pool feedback."""

    SUCCESS = "success"
    APPLICATION_RISK = "application_risk"
    CAPTCHA_FAILED = "captcha_failed"
    MAILBOX_TIMEOUT = "mailbox_timeout"
    SERVICE_UNAVAILABLE = "service_unavailable"
    UI_ERROR = "ui_error"
    TRANSPORT_ERROR = "transport_error"

    def __str__(self):
        return self.value


@dataclass(frozen=True, slots=True)
class RegistrationResult:
    """Application-flow result; it is separate from proxy connectivity health."""

    outcome: RegistrationOutcome
    detail: str = ""
    stage: str = ""
    elapsed_ms: float = 0.0

    @property
    def success(self):
        return self.outcome is RegistrationOutcome.SUCCESS

    @property
    def failure_class(self):
        if self.success:
            return None
        return self.outcome.value

    def as_dict(self):
        return {
            "success": self.success,
            "outcome": self.outcome.value,
            "detail": self.detail,
            "stage": self.stage,
            "elapsed_ms": self.elapsed_ms,
            "failure_class": self.failure_class,
        }

    def __bool__(self):
        return self.success
