"""Validate variables before they reach shell files or guest processes."""

from typing import Annotated

from pydantic import AfterValidator, Field

# These names describe the saved topology. User values must not change script routing.
CONTEXT_VARIABLES = frozenset(
    {
        "NODE_NAME",
        "SSH_USER",
        "NODE_IP",
        "PRIMARY_NODE_IP",
        "NODE_COUNT",
        "KUBERNETES_VERSION",
        "SHARED_DIR",
    }
)

EnvironmentName = Annotated[str, Field(pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")]
EnvironmentValue = Annotated[str, Field(strict=True, pattern=r"^[^\x00\r\n]*$")]


def protect_context(values: dict[str, str]) -> dict[str, str]:
    """Keep Labstack's script context separate from user-defined variables."""
    if CONTEXT_VARIABLES.intersection(values):
        raise ValueError("Environment cannot override Labstack context variables.")
    return values


Environment = Annotated[
    dict[EnvironmentName, EnvironmentValue], AfterValidator(protect_context)
]
