import json
import shutil
from unittest.mock import Mock

import pytest
import yaml
from pydantic import ValidationError

from labstack import (
    cloud_init,
    kubernetes,
    qemu,
    ssh,
)
from labstack.inventory import nodes, read_config
from labstack.models.config import Config
from labstack.models.machine import Node
from labstack.models.state import State
from labstack.settings import get_settings


def test_named_networks_and_published_ports_reach_cloud_init_qemu_and_ssh(
    tmp_path, monkeypatch
):
    definition = yaml.safe_load((get_settings().root / "machines.yml").read_text())
    definition["networks"] = {
        "external": {"driver": "nat"},
        "internal": {"driver": "socket", "subnet": "172.28.16.0/25"},
    }
    for index, machine in enumerate(definition["machines"].values()):
        machine["networks"] = {
            "external": {},
            "internal": {"ipv4_address": f"172.28.16.{10 + index}"},
        }
        machine["ports"] = [f"{53222 + index * 10}:22"]
    definition["machines"]["cp"]["ports"] += ["17443:6443", "18080:8080"]
    inventory = tmp_path / "machines.yml"
    inventory.write_text(yaml.safe_dump(definition))
    config = read_config(inventory)
    state = State(
        config=config,
        instance_id="test",
        base_images={node.name: tmp_path / "base.qcow2" for node in nodes(config)},
    )
    directory = tmp_path / "state"
    directory.mkdir()
    monkeypatch.setattr(get_settings(), "state_dir", directory)
    cp, worker = nodes(config)

    interfaces = cloud_init.network_config(cp)["ethernets"]
    assert cp.address == "172.28.16.10"
    assert worker.address == "172.28.16.11"
    assert interfaces["cluster0"]["addresses"] == ["172.28.16.10/25"]
    assert interfaces["nat0"]["dhcp4"] is True
    assert "set-name" not in interfaces["nat0"]
    assert "routes" not in interfaces["cluster0"]
    command = qemu.qemu_command(cp, state, tmp_path)
    assert (
        "user,id=nat,hostfwd=tcp:127.0.0.1:53222-:22,"
        "hostfwd=tcp:127.0.0.1:17443-:6443,hostfwd=tcp:127.0.0.1:18080-:8080" in command
    )
    ssh.write_ssh_config(state)
    ssh_config = (directory / "ssh_config").read_text()
    assert "Host cp\n  Port 53222" in ssh_config
    assert "Host w1\n  Port 53232" in ssh_config
    hosts = cloud_init.cloud_config(cp, state, "ssh-ed25519 TEST project")[
        "write_files"
    ][0]
    assert "172.28.16.11 w1" in hosts["content"]


def test_kubeconfig_uses_the_published_control_plane_port(tmp_path, monkeypatch):
    definition = read_config().model_dump()
    definition["machines"]["cp"]["ports"] = ["52222:22", "17443:6443"]
    config = Config.model_validate(definition)
    state = State(
        config=config,
        instance_id="test",
        base_images={node.name: tmp_path / "base.qcow2" for node in nodes(config)},
    )
    directory = tmp_path / "state"
    directory.mkdir()
    (directory / "state.json").write_text(state.model_dump_json())
    monkeypatch.setattr(get_settings(), "root", tmp_path)
    monkeypatch.setattr(get_settings(), "state_dir", directory)
    guest_config = {"clusters": [{"cluster": {"server": "https://192.168.76.10:6443"}}]}
    monkeypatch.setattr(
        kubernetes, "remote", Mock(return_value=json.dumps(guest_config))
    )

    kubernetes.export_kubeconfig(state)

    exported = json.loads((tmp_path / "kubeconfig").read_text())
    assert exported["clusters"][0]["cluster"]["server"] == "https://127.0.0.1:17443"


def test_kubernetes_helpers_find_the_control_plane_after_a_worker(
    tmp_path, monkeypatch
):
    definition = read_config().model_dump()
    machines = definition["machines"]
    definition["machines"] = {name: machines[name] for name in ("w1", "cp", "w2")}
    config = Config.model_validate(definition)
    state = State(
        config=config,
        instance_id="test",
        base_images=dict.fromkeys(config.active_machines, tmp_path / "base"),
    )
    monkeypatch.setattr(get_settings(), "root", tmp_path)
    guest_config = {"clusters": [{"cluster": {"server": "https://192.168.76.10:6443"}}]}
    remote_mock = Mock(return_value=json.dumps(guest_config))
    monkeypatch.setattr(kubernetes, "remote", remote_mock)

    assert config.kubernetes_enabled is True
    kubernetes.export_kubeconfig(state)

    assert remote_mock.call_args.args[0].name == "cp"
    exported = json.loads((tmp_path / "kubeconfig").read_text())
    assert exported["clusters"][0]["cluster"]["server"] == "https://127.0.0.1:16443"


@pytest.mark.parametrize(
    "failure", ["missing_api", "pod_overlap", "second_control_plane"]
)
def test_reordered_kubernetes_recipe_still_validates_its_requirements(failure):
    definition = read_config().model_dump()
    machines = definition["machines"]
    definition["machines"] = {name: machines[name] for name in ("w1", "cp", "w2")}
    if failure == "missing_api":
        machines["cp"]["ports"] = ["52222:22"]
    elif failure == "pod_overlap":
        definition["networks"]["lab"]["subnet"] = "10.244.0.0/24"
        for index, machine in enumerate(machines.values()):
            machine["networks"]["lab"]["ipv4_address"] = f"10.244.0.{10 + index}"
    else:
        machines["w1"]["scripts"] = ["provision/kubernetes/control-plane.sh"]
        machines["w1"]["depends_on"] = {}
        machines["w1"]["healthcheck"] = None
        machines["w1"]["ports"] = ["52232:22", "17443:6443"]
    with pytest.raises(ValidationError):
        Config.model_validate(definition)


def test_long_machine_names_and_project_paths_use_short_private_sockets(
    tmp_path, monkeypatch
):
    definition = read_config().model_dump()
    for machine in definition["machines"].values():
        machine.update(scripts=[], healthcheck=None, depends_on={})
    name = "a" * 63
    definition["machines"][name] = definition["machines"].pop("cp")
    config = Config.model_validate(definition)
    settings = get_settings()
    monkeypatch.setattr(
        settings, "state_dir", tmp_path / ("long-project-" * 12) / ".state"
    )
    state = State(
        config=config,
        instance_id="test",
        base_images=dict.fromkeys(config.active_machines, tmp_path / "base"),
    )
    node = next(node for node in nodes(config) if node.name == name)
    command = qemu.qemu_command(node, state, tmp_path)
    monitor = command[command.index("-qmp") + 1]
    path = monitor.removeprefix("unix:").split(",", 1)[0]

    assert len(path.encode()) <= 103
    qemu.prepare_runtime_directory()
    try:
        with qemu.socket.socket(
            qemu.socket.AF_UNIX, qemu.socket.SOCK_STREAM
        ) as listener:
            listener.bind(path)
            assert settings.runtime_dir.stat().st_mode & 0o777 == 0o700
    finally:
        shutil.rmtree(settings.runtime_dir)


def test_runtime_socket_directories_are_separate_for_each_environment(
    tmp_path, monkeypatch
):
    settings = get_settings()
    monkeypatch.setattr(settings, "state_dir", tmp_path / "first" / ".state")
    first = settings.runtime_dir
    monkeypatch.setattr(settings, "state_dir", tmp_path / "second" / ".state")

    assert settings.runtime_dir != first


def test_runtime_directory_rejects_a_link_without_changing_its_target(
    tmp_path, monkeypatch
):
    settings = get_settings()
    monkeypatch.setattr(settings, "state_dir", tmp_path / ".state")
    target = tmp_path / "unrelated"
    target.mkdir(mode=0o755)
    original_mode = target.stat().st_mode
    settings.runtime_dir.symlink_to(target)
    try:
        with pytest.raises(ValueError, match="Unsafe QEMU runtime"):
            qemu.prepare_runtime_directory()
        assert target.stat().st_mode == original_mode
    finally:
        settings.runtime_dir.unlink()


@pytest.mark.parametrize("address", ["192.168.76.0", "192.168.76.255", "192.168.77.10"])
def test_private_ip_must_be_a_usable_host_in_the_named_subnet(address):
    definition = read_config().model_dump()
    definition["machines"]["cp"]["networks"]["lab"]["ipv4_address"] = address

    with pytest.raises(ValidationError, match="host address"):
        Config.model_validate(definition)


def test_two_machines_cannot_share_the_same_private_ip():
    definition = read_config().model_dump()
    definition["machines"]["w1"]["networks"]["lab"]["ipv4_address"] = "192.168.76.10"

    with pytest.raises(ValidationError, match="Private IPs must be unique"):
        Config.model_validate(definition)


@pytest.mark.parametrize("prefix", ["10.0.2", "10.244.0", "10.96.0"])
def test_private_subnet_cannot_overlap_nat_pods_or_services(prefix):
    definition = read_config().model_dump()
    definition["networks"]["lab"]["subnet"] = f"{prefix}.0/24"
    for index, machine in enumerate(definition["machines"].values()):
        machine["networks"]["lab"]["ipv4_address"] = f"{prefix}.{10 + index}"

    with pytest.raises(ValidationError, match="reserved network"):
        Config.model_validate(definition)


@pytest.mark.parametrize("subnet", ["8.8.8.0/24", "127.0.0.0/24", "192.168.76.1/24"])
def test_network_requires_a_private_subnet_with_a_network_address(subnet):
    definition = read_config().model_dump()
    definition["networks"]["lab"]["subnet"] = subnet

    with pytest.raises(ValidationError):
        Config.model_validate(definition)


@pytest.mark.parametrize("port", ["52222:22", "16443:8080"])
def test_all_published_ports_must_be_unique_on_the_mac(port):
    definition = read_config().model_dump()
    definition["machines"]["w1"]["ports"] = (
        ["53232:22", port] if port.endswith(":8080") else [port]
    )

    with pytest.raises(ValidationError, match="ports must be unique"):
        Config.model_validate(definition)


@pytest.mark.parametrize(
    "port",
    [
        "80:80",
        "65536:80",
        "18080:0",
        "18080:65536",
        "0.0.0.0:18080:80",
        "18080:80/udp",
        "$(id):80",
    ],
)
def test_invalid_or_unsupported_port_mappings_are_rejected(port):
    definition = read_config().model_dump()
    definition["machines"]["w1"]["ports"] = ["52232:22", port]

    with pytest.raises(ValidationError):
        Config.model_validate(definition)


@pytest.mark.parametrize("ports", [["18080:80"], ["52232:22", "53232:22"]])
def test_each_machine_needs_one_unambiguous_ssh_mapping(ports):
    definition = read_config().model_dump()
    definition["machines"]["w1"]["ports"] = ports

    with pytest.raises(ValidationError, match="SSH|Guest ports"):
        Config.model_validate(definition)


def test_control_plane_requires_a_published_api_port():
    definition = read_config().model_dump()
    definition["machines"]["cp"]["ports"] = ["52222:22"]

    with pytest.raises(ValidationError, match="control plane requires"):
        Config.model_validate(definition)


@pytest.mark.parametrize("name", ["missing", "nat"])
def test_every_machine_must_reference_both_defined_networks(name):
    definition = read_config().model_dump()
    definition["machines"]["cp"]["networks"] = {name: {}}

    with pytest.raises(ValidationError, match="reference"):
        Config.model_validate(definition)


@pytest.mark.parametrize("driver", ["bridge", "nat"])
def test_unsupported_network_topologies_are_rejected(driver):
    definition = read_config().model_dump()
    definition["networks"]["lab"]["driver"] = driver

    with pytest.raises(ValidationError):
        Config.model_validate(definition)


def test_nat_addresses_are_managed_by_qemu():
    definition = read_config().model_dump()
    definition["machines"]["cp"]["networks"]["nat"]["ipv4_address"] = "10.0.2.15"

    with pytest.raises(ValidationError, match="DHCP"):
        Config.model_validate(definition)


def test_arbitrary_published_ports_are_checked_before_qemu_starts():
    definition = read_config().model_dump()
    with qemu.socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        published = listener.getsockname()[1]
        # Check our reserved port first, even when the user's default VMs are running.
        definition["machines"]["cp"]["ports"] = (
            f"{published}:8080",
            *definition["machines"]["cp"]["ports"],
        )
        config = Config.model_validate(definition)

        with pytest.raises(
            ValueError, match=f"Host port {published} is already in use"
        ):
            qemu.check_ports(nodes(config)[0])


def test_recently_closed_connections_do_not_block_a_guest_restart():
    # QEMU enables address reuse. The preflight must allow the same TIME_WAIT sockets.
    with qemu.socket.socket() as listener:
        listener.setsockopt(qemu.socket.SOL_SOCKET, qemu.socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        address = listener.getsockname()
        with qemu.socket.socket() as client:
            client.settimeout(2)
            client.connect(address)
            connection, _ = listener.accept()
            connection.close()
            assert client.recv(1) == b""
    definition = nodes(read_config())[0].model_dump()
    definition["ports"] = [f"{address[1]}:22"]

    qemu.check_ports(Node.model_validate(definition))


def test_address_reuse_still_rejects_a_live_listener():
    with qemu.socket.socket() as listener:
        listener.setsockopt(qemu.socket.SOL_SOCKET, qemu.socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        port = listener.getsockname()[1]
        definition = nodes(read_config())[0].model_dump()
        definition["ports"] = [f"{port}:22"]

        with pytest.raises(ValueError, match=f"Host port {port} is already in use"):
            qemu.check_ports(Node.model_validate(definition))
