"""Validate guest accounts used in SSH files, cloud-init and home paths."""

from typing import Annotated

from pydantic import AfterValidator, Field


def require_non_root(value: str) -> str:
    """Keep SSH on a normal account because cloud-init disables direct root access."""
    if value == "root":
        raise ValueError("Use a non-root SSH account; sudo provides root access.")
    return value


# Reject whitespace, path separators and shell text before generating guest access.
GuestUsername = Annotated[
    str,
    Field(pattern=r"^[a-z_][a-z0-9_-]*$", max_length=32),
    AfterValidator(require_non_root),
]
