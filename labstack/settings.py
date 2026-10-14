"""Keep command options and filesystem paths separate from the VM inventory."""

import hashlib
import os
from functools import cache
from pathlib import Path
from typing import Annotated, Literal, Self

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from labstack.models.machine import ProfileName

# The client checksum belongs to this version. Update both when changing Kubernetes.
KUBERNETES_VERSION = "1.37.1"
KUBECTL_SHA256 = "fd65982c97ddad3106754b69ffa196d0e543aa591930ae52aed1adfb92f8c77f"


class Settings(BaseSettings):
    """Load host paths, logging and Task options without reading machines.yml.

    The inventory describes the guests. These settings describe the command running
    on the Mac. Relative paths are resolved against the project, so all modules use
    the same state and cache even when a caller has another working directory.
    """

    model_config = SettingsConfigDict(
        env_prefix="LABSTACK_",
        env_ignore_empty=True,
        extra="ignore",
        populate_by_name=True,
    )
    root: Path = Field(default=Path(__file__).resolve().parents[1])
    state_dir: Path = Path(".state")
    cache_dir: Path = Path(".cache")
    logs_dir: Path = Path("logs")
    log_level: Literal["debug", "info", "warning", "error"] = "info"
    profiles: Annotated[tuple[ProfileName, ...] | None, NoDecode] = Field(
        default=None, validation_alias="PROFILES"
    )
    image_path: Path | None = Field(default=None, validation_alias="IMAGE_PATH")
    kubectl_args: str = Field(
        default="get nodes -o wide", validation_alias="KUBECTL_ARGS"
    )

    @field_validator("profiles", mode="before")
    @classmethod
    def split_profiles(
        cls, value: str | tuple[str, ...] | None
    ) -> tuple[str, ...] | None:
        """Accept comma-separated Task profiles, keeping an omitted override unset."""
        if isinstance(value, str):
            return tuple(part.strip() for part in value.split(","))
        return value

    @model_validator(mode="after")
    def resolve_paths(self) -> Self:
        """Resolve host paths and keep cleanup limited to the project's .state.

        Cache and diagnostic paths may be outside the project, but they must remain
        outside the directory removed by destroy. Resolving symlinks before this
        check prevents a state link from redirecting cleanup to another directory.
        """
        self.root = self.root.expanduser().resolve()
        self.state_dir = (self.root / self.state_dir.expanduser()).resolve()
        self.cache_dir = (self.root / self.cache_dir.expanduser()).resolve()
        self.logs_dir = (self.root / self.logs_dir.expanduser()).resolve()
        # Destroy removes this entire directory. It must never become the project root
        # or another existing directory through an option or a filesystem link.
        if self.state_dir != self.root / ".state":
            raise ValueError("State directory must be the project's .state directory.")
        if self.cache_dir.is_relative_to(
            self.state_dir
        ) or self.logs_dir.is_relative_to(self.state_dir):
            raise ValueError("Cache and logs must stay outside the state directory.")
        if self.image_path is not None:
            self.image_path = self.image_path.expanduser()
        return self

    def machine_dir(self, name: str) -> Path:
        """Separate VM resources from access files and shared recipe output.

        A machine may be named shared. Its disk must never become part of the
        shared directory that provisioning uploads and downloads with rsync.
        """
        return self.state_dir / "machines" / name

    @property
    def runtime_dir(self) -> Path:
        """Keep Unix socket paths short even for deeply nested project folders.

        macOS limits a socket path to 103 bytes. A digest separates environments
        without including their full path or machine names. The directory is created
        with private permissions before QEMU starts; disks remain in the project.
        """
        digest = hashlib.sha256(os.fsencode(self.state_dir)).hexdigest()[:16]
        return Path("/tmp") / f"labstack-{os.getuid()}-{digest}"

    @property
    def reset_file(self) -> Path:
        """Keep reset choices outside the directory that reset removes."""
        return self.root / ".reset.json"


@cache
def get_settings() -> Settings:
    """Load one settings object per command so modules share the same path choices."""
    return Settings()
