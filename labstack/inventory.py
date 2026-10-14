"""Load the VM inventory and turn its active machine definitions into nodes."""

from ipaddress import IPv4Interface
from pathlib import Path
from typing import Any

import yaml

from labstack.models.config import Config
from labstack.models.machine import Node
from labstack.settings import get_settings


class InventoryLoader(yaml.SafeLoader):
    """Keep YAML safe and reject duplicate explicit keys before they hide settings."""

    def compose_mapping_node(self, anchor: str | None) -> yaml.MappingNode:
        """Allow anchor overrides, but reject two explicit definitions of one key.

        Check during composition, before construction expands any merge. PyYAML can
        flatten a nested anchor before constructing its source; checking at that later
        stage would mistake valid inherited overrides for duplicate explicit keys.
        """
        node = super().compose_mapping_node(anchor)
        seen = set()
        for key_node, _ in node.value:
            if key_node.tag == "tag:yaml.org,2002:merge":
                continue
            if (
                not isinstance(key_node, yaml.ScalarNode)
                or key_node.tag != "tag:yaml.org,2002:str"
            ):
                raise ValueError("Inventory mapping keys must be strings.")
            key = key_node.value
            if key in seen:
                raise ValueError(f"Duplicate YAML key: {key}.")
            seen.add(key)
        return node


def nodes(config: Config) -> list[Node]:
    """Build active runtime nodes from the validated inventory in file order.

    Only machines selected by profiles are included. The first active machine hosts the socket
    network hub, and each node receives its private subnet prefix and deterministic
    interface identities without adding generated fields to machines.yml.
    """
    name = config.private_network_name
    subnet = config.networks[name].subnet
    assert subnet is not None
    return [
        Node(
            name=machine_name,
            index=index,
            private_ip=IPv4Interface(
                f"{machine.networks[name].ipv4_address}/{subnet.prefixlen}"
            ),
            **machine.model_dump(),
        )
        for index, (machine_name, machine) in enumerate(config.machines.items())
        if machine_name in config.active_machines
    ]


def select_node(config: Config, name: str) -> Node:
    """Find an active guest by the exact name saved from machines.yml.

    Connection commands use saved state so they keep matching the existing SSH entries
    and disks. The optional second worker is excluded when it has not been started.
    Listing active names helps callers correct a typo or choose an available machine.
    """
    machines = nodes(config)
    for node in machines:
        if node.name == name:
            return node
    available = ", ".join(node.name for node in machines)
    raise ValueError(
        f"Machine {name!r} is not active in this lab. Available machines: {available}."
    )


def primary_node(config: Config) -> Node:
    """Use the recipe's control plane, or the first guest for other workloads.

    Provisioning scripts and Kubernetes helpers must agree on the server address.
    Keeping this choice separate from the network hub lets YAML order stay an
    infrastructure choice instead of silently changing the Kubernetes endpoint.
    """
    name = config.control_plane_name or next(iter(config.active_machines))
    return select_node(config, name)


def load_definition(path: Path | None = None) -> Any:
    """Expand safe YAML anchors before profile selection and schema validation."""
    settings = get_settings()
    inventory = path or settings.root / "machines.yml"
    try:
        return yaml.load(inventory.read_text(), Loader=InventoryLoader)
    except yaml.YAMLError as error:
        raise ValueError(f"Invalid YAML in {inventory.name}: {error}") from error


def read_config(path: Path | None = None) -> Config:
    """Load and validate machines.yml without resolving remote image names.

    Keeping this step offline allows configuration errors to be reported before any
    network request or VM work. Box resolution is a separate creation-time operation;
    reading an inventory never changes its image selection.
    """
    return Config.model_validate(load_definition(path))


def resolve_config(existing: Config | None, profiles: tuple[str, ...] | None) -> Config:
    """Apply profile selection and protect the choices stored with existing VMs.

    An omitted override keeps the saved profiles rather than the current file
    default. Other inventory changes require destroying the environment first, because
    existing overlays and network identities were created from the saved definition.
    """
    definition = load_definition()
    if not isinstance(definition, dict):
        raise ValueError("machines.yml requires a mapping at its root.")
    if profiles is not None:
        definition["profiles"] = profiles
    elif existing:
        definition["profiles"] = existing.profiles
    config = Config.model_validate(definition)
    # Mapping equality ignores order, but QEMU MAC indexes and the hub do not.
    if existing and (
        config != existing or list(config.machines) != list(existing.machines)
    ):
        raise ValueError("Run 'task destroy' before changing machines.yml or PROFILES.")
    return config
