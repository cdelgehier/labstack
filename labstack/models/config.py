"""Validate the complete machine inventory before creating host resources."""

from ipaddress import IPv4Network
from typing import Any, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    HttpUrl,
    field_validator,
    model_validator,
)

from labstack.models.machine import Machine, MachineName, ProfileName
from labstack.models.network import Network
from labstack.planning import prepare_plan


class Config(BaseModel):
    """Validate choices that must stay fixed for the life of the environment.

    The complete inventory is checked before downloading images or creating disks.
    Inactive machine definitions are also validated, so enabling a second worker cannot
    introduce an unnoticed address, port or script-path conflict.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)
    boxes_catalogue: HttpUrl
    profiles: tuple[ProfileName, ...] = ()
    machines: dict[MachineName, Machine] = Field(min_length=1, max_length=16)
    networks: dict[str, Network]

    @model_validator(mode="before")
    @classmethod
    def remove_extension_blocks(cls, value: Any) -> Any:
        """Ignore root x-* blocks after YAML has expanded their anchors.

        Reusable blocks are authoring helpers, not guest settings. All other unknown
        keys still fail validation, so a spelling error cannot silently change a lab.
        """
        if isinstance(value, dict):
            return {
                key: item
                for key, item in value.items()
                if not (isinstance(key, str) and key.startswith("x-"))
            }
        return value

    @field_validator("boxes_catalogue")
    @classmethod
    def require_catalogue_base_url(cls, value: HttpUrl) -> HttpUrl:
        """Append target filenames to an HTTPS base URL, without URL parameters."""
        if (
            value.scheme != "https"
            or value.query
            or value.fragment
            or value.username
            or value.password
        ):
            raise ValueError(
                "boxes_catalogue requires an HTTPS base URL without credentials, query or fragment."
            )
        return value

    @model_validator(mode="after")
    def validate_machines(self) -> Self:
        """Select optional guests without tying the inventory to Kubernetes workers."""
        available = {
            profile
            for machine in self.machines.values()
            for profile in machine.profiles
        }
        unknown = set(self.profiles) - available
        if unknown:
            raise ValueError(f"Unknown profiles: {', '.join(sorted(unknown))}.")
        if not self.active_machines:
            raise ValueError("Select at least one active machine.")
        control_planes = [
            name
            for name, machine in self.active_machines.items()
            if "provision/kubernetes/control-plane.sh" in machine.scripts
        ]
        if len(control_planes) > 1:
            raise ValueError("Select only one Kubernetes control plane.")
        return self

    @model_validator(mode="after")
    def validate_dependencies(self) -> Self:
        """Validate inactive recipes too, then check the selected startup graph.

        Optional references may skip an inactive profile, but they cannot hide a typo
        or a setup cycle. The full graph checks dormant recipes before they are enabled.
        """
        for name, machine in self.machines.items():
            for dependency_name, dependency in machine.depends_on.items():
                if dependency_name not in self.machines:
                    raise ValueError(
                        f"Unknown dependency {dependency_name} for {name}."
                    )
                if dependency_name == name:
                    raise ValueError(f"Machine {name} cannot depend on itself.")
                if (
                    dependency.condition == "healthy"
                    and self.machines[dependency_name].healthcheck is None
                ):
                    raise ValueError(
                        f"Dependency {dependency_name} requires a healthcheck."
                    )
        prepare_plan(self.machines)
        prepare_plan(self.active_machines)
        return self

    @model_validator(mode="after")
    def validate_network(self) -> Self:
        """Keep guests on one private subnet and avoid conflicts on the Mac."""
        drivers = [network.driver for network in self.networks.values()]
        if sorted(drivers) != ["nat", "socket"]:
            raise ValueError("Define exactly one nat network and one socket network.")
        subnet = self.networks[self.private_network_name].subnet
        assert subnet is not None
        addresses = []
        for machine in self.machines.values():
            if set(machine.networks) != set(self.networks):
                raise ValueError("Each machine must reference both defined networks.")
            for name, attachment in machine.networks.items():
                if self.networks[name].driver == "nat":
                    if attachment.ipv4_address is not None:
                        raise ValueError(
                            "The NAT interface uses DHCP; omit ipv4_address."
                        )
                    continue
                address = attachment.ipv4_address
                if (
                    address is None
                    or address not in subnet
                    or address
                    in {
                        subnet.network_address,
                        subnet.broadcast_address,
                    }
                ):
                    raise ValueError(
                        "Private IP must be a usable host address in subnet."
                    )
                addresses.append(address)
        if len(set(addresses)) != len(addresses):
            raise ValueError("Private IPs must be unique.")
        # The private link has no router. It must stay separate from the other networks.
        reserved = [IPv4Network("10.0.2.0/24")]
        if self.kubernetes_enabled:
            reserved.extend([IPv4Network("10.244.0.0/16"), IPv4Network("10.96.0.0/12")])
        for network in reserved:
            if subnet.overlaps(network):
                raise ValueError(
                    f"Private subnet {subnet} overlaps reserved network {network}."
                )
        control_plane = self.control_plane_name
        if (
            control_plane
            and self.active_machines[control_plane].host_port(6443) is None
        ):
            raise ValueError("The control plane requires a published API port (6443).")
        ports = [
            port.published
            for machine in self.machines.values()
            for port in machine.ports
        ]
        if len(set(ports)) != len(ports):
            raise ValueError("Published ports must be unique on the Mac.")
        return self

    @property
    def active_machines(self) -> dict[str, Machine]:
        """Keep unprofiled guests and those matching at least one enabled profile.

        File order remains stable. Inactive definitions are kept in the full inventory
        for validation and stable MAC numbering, but receive no disks or image pins.
        """
        return {
            name: machine
            for name, machine in self.machines.items()
            if not machine.profiles or set(machine.profiles).intersection(self.profiles)
        }

    @property
    def private_network_name(self) -> str:
        """Find the shared socket network without depending on its chosen name."""
        return next(
            name for name, net in self.networks.items() if net.driver == "socket"
        )

    @property
    def kubernetes_enabled(self) -> bool:
        """Enable Kubernetes helpers only when its control plane recipe is selected."""
        return self.control_plane_name is not None

    @property
    def control_plane_name(self) -> str | None:
        """Find the Kubernetes server by its recipe, independently of file order.

        The first active machine owns the QEMU hub, but it may be a worker or an
        unrelated service. Only the control plane script identifies the API server.
        """
        return next(
            (
                name
                for name, machine in self.active_machines.items()
                if "provision/kubernetes/control-plane.sh" in machine.scripts
            ),
            None,
        )
