"""Describe machine resources, provisioning lists and generated guest identities."""

from ipaddress import IPv4Interface
from pathlib import Path
from typing import Annotated, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from labstack.models.access import GuestUsername
from labstack.models.environment import Environment
from labstack.models.image import BoxReference
from labstack.models.network import NetworkAttachment, PublishedPort
from labstack.models.provisioning import Dependency, Healthcheck

MachineName = Annotated[str, Field(pattern=r"^[a-z][a-z0-9-]{0,61}[a-z0-9]$|^[a-z]$")]
ProfileName = Annotated[str, Field(pattern=r"^[a-z][a-z0-9-]*$")]


class Machine(BaseModel):
    """Describe a guest's resources, image reference and ordered script list.

    The scripts decide what software is installed. Machine names identify hostnames,
    SSH entries and disk directories, so they must be safe to use in all three places.
    Network addresses and ports are checked against the complete inventory.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)
    box: BoxReference
    # Older saved labs used Ubuntu implicitly. New inventories should name the box account.
    ssh_user: GuestUsername = "ubuntu"
    env_file: tuple[str, ...] = Field(default=(), max_length=16)
    environment: Environment = Field(default_factory=dict)
    profiles: tuple[ProfileName, ...] = ()
    networks: dict[str, NetworkAttachment]
    ports: tuple[PublishedPort, ...]
    scripts: tuple[str, ...] = ()
    healthcheck: Healthcheck | None = None
    depends_on: dict[MachineName, Dependency] = Field(default_factory=dict)
    cpus: int = Field(default=2, ge=1, le=16)
    memory_mib: int = Field(default=2048, ge=1024, le=32768)
    disk_gib: int = Field(default=20, ge=16, le=512)

    @field_validator("env_file", mode="before")
    @classmethod
    def accept_environment_file(cls, value: str | tuple[str, ...]) -> tuple[str, ...]:
        """Accept one dotenv path as well as an ordered list, as in Compose."""
        return (value,) if isinstance(value, str) else value

    @field_validator("env_file")
    @classmethod
    def require_environment_paths(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        """Keep dotenv files relative to the inventory, with no parent traversal."""
        for value in values:
            path = Path(value)
            if (
                not path.parts
                or path.is_absolute()
                or ".." in path.parts
                or "\x00" in value
            ):
                raise ValueError(
                    "Use relative env_file paths inside the inventory directory."
                )
        return values

    @model_validator(mode="after")
    def validate_ports(self) -> Self:
        """Keep the SSH endpoint unambiguous and allow other guest TCP services."""
        targets = [port.target for port in self.ports]
        if len(set(targets)) != len(targets):
            raise ValueError("Guest ports must be unique within each machine.")
        if 22 not in targets:
            raise ValueError("Each machine requires a published SSH port (22).")
        return self

    @model_validator(mode="after")
    def validate_checkpoints(self) -> Self:
        """Use exact script paths for gates, so renaming a script cannot move a wait."""
        if len(set(self.scripts)) != len(self.scripts):
            raise ValueError("Script paths must be unique within each machine.")
        if (
            self.healthcheck
            and self.healthcheck.after is not None
            and self.healthcheck.after not in self.scripts
        ):
            raise ValueError(
                "healthcheck.after must name a script in this machine's list."
            )
        for dependency in self.depends_on.values():
            if dependency.before is not None and dependency.before not in self.scripts:
                raise ValueError(
                    "depends_on.before must name a script in this machine's list."
                )
        return self

    def host_port(self, target: int) -> int | None:
        """Find the Mac port used to reach a particular guest service."""
        for port in self.ports:
            if port.target == target:
                return port.published
        return None

    @field_validator("scripts")
    @classmethod
    def require_script_paths(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        """Keep executable script paths inside the project's provisioning directory."""
        for value in values:
            path = Path(value)
            if (
                path.is_absolute()
                or ".." in path.parts
                or not path.parts
                or path.parts[0] != "provision"
                or path.suffix != ".sh"
            ):
                raise ValueError("Use relative .sh script paths under provision/.")
        return values


class Node(Machine):
    """Add generated runtime identities to one active machine definition.

    Inventory order determines the QEMU hub. The index also produces separate MAC
    addresses for each interface; it does not choose provisioning.
    The private address includes the shared network's prefix for cloud-init.
    """

    name: MachineName
    index: int = Field(ge=0, le=15)
    private_ip: IPv4Interface

    @property
    def address(self) -> str:
        """Return the address used by Kubernetes and other guests."""
        return str(self.private_ip.ip)

    @property
    def ssh_port(self) -> int:
        """Return the SSH port forwarded on the Mac."""
        port = self.host_port(22)
        assert port is not None
        return port

    @property
    def api_port(self) -> int | None:
        """Return the published API port when the inventory includes it."""
        return self.host_port(6443)

    @property
    def nat_mac(self) -> str:
        """Match the Internet interface in cloud-init."""
        return f"52:54:00:76:00:{self.index:02x}"

    @property
    def cluster_mac(self) -> str:
        """Match the private interface in cloud-init."""
        return f"52:54:00:76:01:{self.index:02x}"
