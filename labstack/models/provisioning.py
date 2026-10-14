"""Describe guest readiness commands and the script gates that depend on them."""

import re
from typing import Annotated, Any, Literal

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, field_validator


def duration_seconds(value: Any) -> Any:
    """Accept seconds or short YAML durations such as 500ms, 5s and 2m.

    Models keep seconds internally so subprocess timeouts and retry delays share one
    representation. Unsupported units fail early instead of creating a long wait.
    """
    if isinstance(value, bool):
        raise ValueError("A duration must be a number of seconds or a short duration.")
    if isinstance(value, str):
        match = re.fullmatch(r"(\d+(?:\.\d+)?)(ms|s|m)", value)
        if not match:
            raise ValueError("Use seconds or a duration with ms, s or m.")
        scales = {"ms": 0.001, "s": 1, "m": 60}
        return float(match[1]) * scales[match[2]]
    return value


Duration = Annotated[float, BeforeValidator(duration_seconds)]


class Healthcheck(BaseModel):
    """Check a guest service once during each provisioned startup.

    The command runs through the guest's SSH account. CMD preserves literal arguments;
    CMD-SHELL explicitly allows shell syntax. An after checkpoint can release clients
    before the server finishes its later scripts, avoiding circular setup waits.
    Durations limit each attempt; retries limit the total number of attempts.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)
    test: tuple[str, ...]
    after: str | None = None
    interval: Duration = Field(default=5, gt=0, le=300)
    timeout: Duration = Field(default=10, gt=0, le=300)
    retries: int = Field(default=12, ge=1, le=120, strict=True)
    start_period: Duration = Field(default=0, ge=0, le=600)

    @field_validator("test", mode="before")
    @classmethod
    def expand_shell_command(cls, value: Any) -> Any:
        """Treat a plain command string as an explicit shell healthcheck."""
        if isinstance(value, str):
            return ("CMD-SHELL", value)
        return value

    @field_validator("test")
    @classmethod
    def validate_command(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        """Require a command mode and reject empty commands or embedded NUL bytes."""
        if (
            len(value) < 2
            or value[0] not in {"CMD", "CMD-SHELL"}
            or (value[0] == "CMD-SHELL" and len(value) != 2)
            or not value[1].strip()
            or any("\0" in argument for argument in value)
        ):
            raise ValueError("test requires CMD arguments or one CMD-SHELL command.")
        return value


class Dependency(BaseModel):
    """Gate one guest's script on another guest's startup or provisioning result.

    With no before path, the first script waits. A checkpoint path allows common setup
    to run first. Optional dependencies are skipped only when their guest is inactive;
    an active dependency still has to succeed.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)
    condition: Literal["started", "healthy", "completed"] = "started"
    before: str | None = None
    required: bool = Field(default=True, strict=True)
