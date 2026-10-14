"""Validate named networks and localhost TCP port mappings."""

import re
from ipaddress import IPv4Address, IPv4Network
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Network(BaseModel):
    """Define a named network that all active machines can reference.

    QEMU manages addresses and routing on the NAT link. The socket link has no router,
    so its private subnet must be explicit and must not overlap NAT or workload ranges.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)
    driver: Literal["nat", "socket"]
    subnet: IPv4Network | None = None

    @model_validator(mode="after")
    def validate_subnet(self) -> Self:
        """Let QEMU manage NAT and require a private subnet for the socket network."""
        if self.driver == "nat":
            if self.subnet is not None:
                raise ValueError("QEMU manages the NAT subnet; omit subnet for nat.")
            return self
        if self.subnet is None:
            raise ValueError("A socket network requires a subnet.")
        private_subnets = (
            IPv4Network("10.0.0.0/8"),
            IPv4Network("172.16.0.0/12"),
            IPv4Network("192.168.0.0/16"),
        )
        if not any(self.subnet.subnet_of(subnet) for subnet in private_subnets):
            raise ValueError("The socket network must use a private IPv4 subnet.")
        return self


class NetworkAttachment(BaseModel):
    """Assign a machine to a declared network.

    The NAT attachment uses DHCP and has no configured address. The socket attachment
    supplies a static IPv4 address that is checked against the inventory's shared subnet.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)
    ipv4_address: IPv4Address | None = None


class PublishedPort(BaseModel):
    """Forward one localhost TCP port through a guest's QEMU NAT interface.

    Inventory strings use HOST:GUEST syntax. Saved state keeps both values as validated
    integers, so QEMU arguments cannot contain injected addresses or extra options.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)
    published: int = Field(ge=1024, le=65535, strict=True)
    target: int = Field(ge=1, le=65535, strict=True)

    @model_validator(mode="before")
    @classmethod
    def parse_mapping(cls, value: Any) -> Any:
        """Read HOST:GUEST strings while keeping structured data in saved state."""
        if isinstance(value, str):
            match = re.fullmatch(r"([0-9]+):([0-9]+)", value)
            if match is None:
                raise ValueError("Use a TCP port mapping in HOST:GUEST format.")
            return {"published": int(match[1]), "target": int(match[2])}
        return value
