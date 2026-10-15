import os
import shlex
import shutil
import subprocess
from unittest.mock import Mock

import pytest
from pydantic import ValidationError

from labstack import (
    cluster,
    images,
    inventory,
    provisioning,
)
from labstack.inventory import nodes, read_config
from labstack.models.config import Config
from labstack.models.state import State
from labstack.settings import get_settings


class ScriptRunnerSpy:
    """Record guest work without starting SSH or a VM."""

    def __init__(self, fail_on=None):
        self.steps = []
        self.fail_on = fail_on

    def __call__(self, node, path, state):
        self.steps.append((node.name, path))
        if path == self.fail_on:
            raise ValueError("Provisioning failed")


class LocalSSHFake:
    """Run the guest shell locally to check the actual script and its environment."""

    def __init__(self, shared):
        self.shared = shared

    def __call__(self, node, command):
        arguments = shlex.split(command)
        assert arguments[:2] == ["sudo", "env"]
        return [
            "env",
            *[
                f"SHARED_DIR={self.shared}"
                if value.startswith("SHARED_DIR=")
                else value
                for value in arguments[2:]
            ],
        ]


class LocalRsyncFake:
    """Keep real rsync options but replace the SSH destination with a directory."""

    def __init__(self, guest, name="cp"):
        self.guest = guest
        self.name = name
        self.run = subprocess.run

    def __call__(self, command, **kwargs):
        arguments = list(command)
        arguments.remove("--rsync-path=sudo rsync")
        ssh_index = arguments.index("-e")
        del arguments[ssh_index : ssh_index + 2]
        for index, value in enumerate(arguments):
            if value.startswith(f"{self.name}:"):
                arguments[index] = f"{self.guest}/"
        return self.run(arguments, **kwargs)


@pytest.mark.skipif(
    not all(shutil.which(command) for command in ("qemu-img", "xorriso", "rsync")),
    reason="Image and transfer tools are missing",
)
def test_shared_named_machine_keeps_its_disk_outside_recipe_transfers(
    tmp_path, monkeypatch
):
    from labstack.cloud_init import prepare_node

    definition = read_config().model_dump()
    for machine in definition["machines"].values():
        machine.update(scripts=[], healthcheck=None, depends_on={})
    definition["machines"]["shared"] = definition["machines"].pop("cp")
    config = Config.model_validate(definition)
    node = next(node for node in nodes(config) if node.name == "shared")
    directory = tmp_path / "state"
    directory.mkdir()
    (directory / "id_ed25519.pub").write_text("ssh-ed25519 TEST")
    monkeypatch.setattr(get_settings(), "state_dir", directory)
    monkeypatch.setattr(get_settings(), "logs_dir", tmp_path)
    firmware = tmp_path / "firmware"
    firmware.mkdir()
    (firmware / "edk2-arm-vars.fd").write_bytes(b"test variables")
    base = tmp_path / "base.qcow2"
    subprocess.run(["qemu-img", "create", "-f", "qcow2", str(base), "64M"], check=True)
    state = State(
        config=config,
        instance_id="test",
        base_images=dict.fromkeys(config.active_machines, base),
    )
    prepare_node(node, state, firmware)
    guest = tmp_path / "guest"
    guest.mkdir()
    monkeypatch.setattr(provisioning, "remote", Mock())
    monkeypatch.setattr(provisioning.subprocess, "run", LocalRsyncFake(guest, "shared"))

    provisioning.sync_shared(node)

    assert not (guest / "disk.qcow2").exists()
    disk = directory / "machines" / "shared" / "disk.qcow2"
    contents = disk.read_bytes()
    (guest / "disk.qcow2").write_bytes(b"recipe output")
    provisioning.sync_shared(node, download=True)
    assert disk.read_bytes() == contents


@pytest.fixture
def script_inventory(tmp_path, monkeypatch):
    """Use real script files with the existing image and network settings."""
    definition = read_config().model_dump()
    for machine in definition["machines"].values():
        machine["scripts"] = []
        machine["healthcheck"] = None
        machine["depends_on"] = {}
    directory = tmp_path / "provision" / "example"
    directory.mkdir(parents=True)
    for filename in ("base.sh", "server.sh", "client.sh", "check.sh"):
        (directory / filename).write_text("#!/usr/bin/env bash\nset -euo pipefail\n")
    monkeypatch.setattr(get_settings(), "root", tmp_path)
    monkeypatch.setattr(get_settings(), "logs_dir", tmp_path)
    monkeypatch.setattr(provisioning, "sync_shared", Mock())
    return definition


@pytest.mark.parametrize("workers", [1, 2])
def test_script_lists_run_in_rounds_before_the_next_step(
    script_inventory, monkeypatch, workers
):
    script_inventory["profiles"] = ["extra-worker"] if workers == 2 else []
    script_inventory["machines"]["cp"]["scripts"] = [
        "provision/example/base.sh",
        "provision/example/server.sh",
        "provision/example/check.sh",
    ]
    script_inventory["machines"]["w1"]["scripts"] = [
        "provision/example/base.sh",
        "provision/example/client.sh",
    ]
    script_inventory["machines"]["w2"]["scripts"] = script_inventory["machines"]["w1"][
        "scripts"
    ]
    config = Config.model_validate(script_inventory)
    state = State(
        config=config,
        instance_id="test",
        base_images={node.name: get_settings().root / "base" for node in nodes(config)},
    )
    runner_spy = ScriptRunnerSpy()
    monkeypatch.setattr(provisioning, "run_script", runner_spy)

    provisioning.provision(state)

    expected = [
        ("cp", "provision/example/base.sh"),
        ("w1", "provision/example/base.sh"),
    ]
    if workers == 2:
        expected.append(("w2", "provision/example/base.sh"))
    expected.extend(
        [("cp", "provision/example/server.sh"), ("w1", "provision/example/client.sh")]
    )
    if workers == 2:
        expected.append(("w2", "provision/example/client.sh"))
    expected.append(("cp", "provision/example/check.sh"))
    assert runner_spy.steps == expected


def test_plain_scripts_do_not_require_kubernetes_or_an_api_port(script_inventory):
    script_inventory["machines"]["cp"]["ports"] = ["52222:22"]
    script_inventory["networks"]["lab"]["subnet"] = "10.244.0.0/24"
    for index, machine in enumerate(script_inventory["machines"].values()):
        machine["networks"]["lab"]["ipv4_address"] = f"10.244.0.{10 + index}"
        machine["scripts"] = ["provision/example/base.sh"]

    config = Config.model_validate(script_inventory)

    assert config.kubernetes_enabled is False
    assert nodes(config)[0].api_port is None


def test_empty_script_lists_leave_the_infrastructure_ready(
    script_inventory, monkeypatch
):
    config = Config.model_validate(script_inventory)
    state = State(
        config=config,
        instance_id="test",
        base_images={node.name: get_settings().root / "base" for node in nodes(config)},
    )
    runner_spy = ScriptRunnerSpy()
    monkeypatch.setattr(provisioning, "run_script", runner_spy)

    provisioning.provision(state)

    assert runner_spy.steps == []


def test_failed_script_stops_the_remaining_work(script_inventory, monkeypatch):
    for machine in script_inventory["machines"].values():
        machine["scripts"] = ["provision/example/base.sh", "provision/example/check.sh"]
    config = Config.model_validate(script_inventory)
    state = State(
        config=config,
        instance_id="test",
        base_images={node.name: get_settings().root / "base" for node in nodes(config)},
    )
    runner_spy = ScriptRunnerSpy(fail_on="provision/example/base.sh")
    monkeypatch.setattr(provisioning, "run_script", runner_spy)

    with pytest.raises(ValueError, match="Provisioning failed"):
        provisioning.provision(state)

    assert runner_spy.steps == [("cp", "provision/example/base.sh")]


@pytest.mark.parametrize(
    "path",
    ["/tmp/root.sh", "provision/../root.sh", "scripts/root.sh", "provision/base/a.txt"],
)
def test_script_paths_must_stay_in_the_provision_directory(script_inventory, path):
    script_inventory["machines"]["cp"]["scripts"] = [path]

    with pytest.raises(ValidationError, match="script"):
        Config.model_validate(script_inventory)


@pytest.mark.parametrize("symlink", [False, True])
def test_unavailable_scripts_fail_before_the_first_guest_command(
    script_inventory, tmp_path, monkeypatch, symlink
):
    path = "provision/example/missing.sh"
    if symlink:
        outside = tmp_path / "outside.sh"
        outside.write_text("echo outside\n")
        (tmp_path / path).symlink_to(outside)
    script_inventory["machines"]["cp"]["scripts"] = ["provision/example/base.sh"]
    script_inventory["machines"]["w1"]["scripts"] = [path]
    config = Config.model_validate(script_inventory)
    state = State(
        config=config,
        instance_id="test",
        base_images={node.name: tmp_path / "base" for node in nodes(config)},
    )
    runner_spy = ScriptRunnerSpy()
    monkeypatch.setattr(provisioning, "run_script", runner_spy)

    with pytest.raises(ValueError, match="script"):
        provisioning.provision(state)

    assert runner_spy.steps == []


@pytest.mark.parametrize("account", ["debian", "vagrant"])
def test_guest_scripts_receive_node_context_and_keep_failure_logs(
    script_inventory, tmp_path, monkeypatch, account
):
    script = tmp_path / "provision/example/server.sh"
    script.write_text(
        'printf "%s\\n" "$NODE_NAME $NODE_IP $PRIMARY_NODE_IP $NODE_COUNT"\n'
        'printf "SSH user: %s\\n" "$SSH_USER"\n'
        'printf "%s" "$NODE_NAME" >"$SHARED_DIR/result"\n'
        'echo "Setup failed" >&2\nexit 9\n'
    )
    script_inventory["machines"]["w1"]["ssh_user"] = account
    config = Config.model_validate(script_inventory)
    state = State(
        config=config,
        instance_id="test",
        base_images={node.name: tmp_path / "base" for node in nodes(config)},
    )
    monkeypatch.setattr(provisioning, "ssh_command", LocalSSHFake(tmp_path))

    with pytest.raises(ValueError, match="server.sh failed on w1"):
        provisioning.run_script(nodes(config)[1], "provision/example/server.sh", state)

    assert (tmp_path / "result").read_text() == "w1"
    log = (tmp_path / "w1-provision.log").read_text()
    assert "w1 192.168.76.11 192.168.76.10 2" in log
    assert f"SSH user: {account}" in log
    assert "Setup failed" in log


def test_worker_context_uses_the_control_plane_even_when_the_worker_is_first(
    tmp_path, monkeypatch
):
    definition = read_config().model_dump()
    machines = definition["machines"]
    definition["machines"] = {name: machines[name] for name in ("w1", "cp", "w2")}
    filename = "provision/example/context.sh"
    machines["w1"]["scripts"] += (filename,)
    config = Config.model_validate(definition)
    script = tmp_path / filename
    script.parent.mkdir(parents=True)
    script.write_text('printf "%s\\n" "$NODE_NAME $PRIMARY_NODE_IP"\n')
    monkeypatch.setattr(get_settings(), "root", tmp_path)
    monkeypatch.setattr(get_settings(), "logs_dir", tmp_path)
    monkeypatch.setattr(provisioning, "ssh_command", LocalSSHFake(tmp_path))
    state = State(
        config=config,
        instance_id="test",
        base_images=dict.fromkeys(config.active_machines, tmp_path / "base"),
    )

    provisioning.run_script(nodes(config)[0], filename, state)

    assert "w1 192.168.76.10" in (tmp_path / "w1-provision.log").read_text()


def test_worker_script_rejects_a_join_command_for_another_control_plane(tmp_path):
    (tmp_path / "join-command").write_text(
        "kubeadm join 192.168.99.10:6443 --token secret\n"
    )

    result = subprocess.run(
        ["bash", str(get_settings().root / "provision/kubernetes/worker.sh")],
        env={"SHARED_DIR": str(tmp_path), "PRIMARY_NODE_IP": "192.168.76.10"},
        text=True,
        capture_output=True,
    )

    assert result.returncode != 0
    assert "unexpected join command" in result.stderr
    assert "secret" not in result.stderr


def test_worker_script_passes_join_arguments_without_shell_evaluation(tmp_path):
    (tmp_path / "join-command").write_text(
        "kubeadm join 192.168.76.10:6443 --token $(touch attacker-file)\n"
    )
    executable = tmp_path / "kubeadm"
    executable.write_text('#!/bin/sh\nprintf "%s\\n" "$@" >"$SHARED_DIR/arguments"\n')
    executable.chmod(0o700)

    result = subprocess.run(
        ["bash", str(get_settings().root / "provision/kubernetes/worker.sh")],
        env={
            "SHARED_DIR": str(tmp_path),
            "PRIMARY_NODE_IP": "192.168.76.10",
            "PATH": f"{tmp_path}:/usr/bin:/bin",
        },
        cwd=tmp_path,
        text=True,
        capture_output=True,
    )

    assert result.returncode == 0
    assert not (tmp_path / "attacker-file").exists()
    assert (tmp_path / "arguments").read_text().splitlines() == [
        "join",
        "192.168.76.10:6443",
        "--token",
        "$(touch",
        "attacker-file)",
    ]


@pytest.mark.skipif(not shutil.which("rsync"), reason="rsync is not installed")
def test_shared_files_keep_private_modes_with_the_system_rsync(tmp_path, monkeypatch):
    state = tmp_path / "state"
    shared = state / "shared"
    shared.mkdir(parents=True)
    (shared / "token").write_text("build-token")
    (shared / "token").chmod(0o644)
    guest = tmp_path / "guest"
    guest.mkdir()
    node = nodes(read_config())[0]
    rsync_fake = LocalRsyncFake(guest)
    monkeypatch.setattr(get_settings(), "state_dir", state)
    monkeypatch.setattr(get_settings(), "logs_dir", tmp_path)
    monkeypatch.setattr(provisioning, "remote", Mock(return_value=""))
    monkeypatch.setattr(provisioning.subprocess, "run", rsync_fake)

    provisioning.sync_shared(node)

    assert (guest / "token").read_text() == "build-token"
    assert guest.stat().st_mode & 0o777 == 0o700
    assert (guest / "token").stat().st_mode & 0o777 == 0o600
    # Equal length and timestamps must not hide a changed join token.
    timestamp = (shared / "token").stat().st_mtime
    (guest / "token").write_text("fresh-token")
    os.utime(guest / "token", (timestamp, timestamp))
    provisioning.sync_shared(node, download=True)
    assert (shared / "token").read_text() == "fresh-token"
    assert (shared / "token").stat().st_mode & 0o777 == 0o600


@pytest.fixture
def guest_environment(script_inventory, tmp_path, monkeypatch, published_box):
    """Keep real saved state and scripts while replacing QEMU and SSH boundaries."""
    directory = tmp_path / "state"
    directory.mkdir()
    (directory / "id_ed25519").write_text("test key")
    monkeypatch.setattr(get_settings(), "state_dir", directory)
    monkeypatch.setattr(cluster, "requirements", Mock(return_value=tmp_path))
    monkeypatch.setattr(cluster, "acquire_image", Mock(return_value=tmp_path / "base"))
    monkeypatch.setattr(cluster, "validate_disk", Mock(return_value=64 * 1024**2))
    monkeypatch.setattr(images, "resolve_box", Mock(return_value=published_box))
    monkeypatch.setattr(cluster, "verify_sha256", Mock())
    monkeypatch.setattr(cluster, "running", Mock(return_value=True))
    monkeypatch.setattr(cluster, "wait_for_guest", Mock())
    config = Config.model_validate(script_inventory)
    monkeypatch.setattr(
        inventory, "load_definition", Mock(return_value=config.model_dump())
    )
    runner_spy = ScriptRunnerSpy()
    monkeypatch.setattr(provisioning, "run_script", runner_spy)
    return runner_spy


def test_no_provision_starts_guests_then_plain_up_provisions_the_same_disks(
    script_inventory, guest_environment, monkeypatch
):
    script_inventory["machines"]["cp"]["scripts"] = ["provision/example/server.sh"]
    script_inventory["machines"]["w1"]["scripts"] = ["provision/example/client.sh"]
    config = Config.model_validate(script_inventory)
    monkeypatch.setattr(
        inventory, "load_definition", Mock(return_value=config.model_dump())
    )

    cluster.up(None, None, no_provision=True)
    infrastructure = cluster.read_state()
    assert infrastructure.provisioned is False
    assert infrastructure.kubernetes_enabled is False
    assert guest_environment.steps == []

    cluster.up(None, None)
    provisioned = cluster.read_state()
    assert provisioned.provisioned is True
    assert provisioned.instance_id == infrastructure.instance_id
    assert provisioned.base_images == infrastructure.base_images
    assert guest_environment.steps == [
        ("cp", "provision/example/server.sh"),
        ("w1", "provision/example/client.sh"),
    ]


def test_no_provision_does_not_require_script_files_but_plain_up_checks_them(
    script_inventory, guest_environment, monkeypatch
):
    script_inventory["machines"]["cp"]["scripts"] = ["provision/example/missing.sh"]
    config = Config.model_validate(script_inventory)
    monkeypatch.setattr(
        inventory, "load_definition", Mock(return_value=config.model_dump())
    )

    cluster.up(None, None, no_provision=True)
    assert cluster.read_state().provisioned is False

    with pytest.raises(ValueError, match="script"):
        cluster.up(None, None)
    assert guest_environment.steps == []
    assert cluster.read_state().provisioned is False


@pytest.mark.parametrize("initially_provisioned", [False, True])
def test_failed_provisioning_is_not_reported_as_completed(
    script_inventory, guest_environment, monkeypatch, initially_provisioned
):
    script_inventory["machines"]["cp"]["scripts"] = ["provision/example/server.sh"]
    config = Config.model_validate(script_inventory)
    monkeypatch.setattr(
        inventory, "load_definition", Mock(return_value=config.model_dump())
    )
    if initially_provisioned:
        cluster.up(None, None)
        assert cluster.read_state().provisioned is True
    guest_environment.fail_on = "provision/example/server.sh"

    with pytest.raises(ValueError, match="Provisioning failed"):
        cluster.up(None, None)

    assert cluster.read_state().provisioned is False


def test_skipping_scripts_does_not_erase_previous_provisioning(
    guest_environment,
):
    cluster.up(None, None)
    assert cluster.read_state().provisioned is True

    cluster.up(None, None, no_provision=True)

    assert cluster.read_state().provisioned is True


def test_kubernetes_helpers_need_completed_provisioning(tmp_path):
    config = read_config()
    state = State(
        config=config,
        instance_id="test",
        base_images={node.name: tmp_path / "base" for node in nodes(config)},
    )

    assert state.kubernetes_enabled is False
    assert state.model_copy(update={"provisioned": True}).kubernetes_enabled is True


def test_no_provision_skips_healthchecks_then_plain_up_runs_them(
    script_inventory, guest_environment, monkeypatch
):
    script_inventory["machines"]["cp"]["healthcheck"] = {"test": ["CMD", "true"]}
    config = Config.model_validate(script_inventory)
    monkeypatch.setattr(
        inventory, "load_definition", Mock(return_value=config.model_dump())
    )
    health = Mock()
    monkeypatch.setattr(provisioning, "wait_for_health", health)

    cluster.up(None, None, no_provision=True)
    health.assert_not_called()

    cluster.up(None, None)

    health.assert_called_once()
    assert cluster.read_state().provisioned is True
