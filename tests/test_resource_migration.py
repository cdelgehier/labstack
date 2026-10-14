"""Protect existing VM resources while moving them away from shared recipe files."""

import json
import os
import shutil
import socket
import tempfile
import threading
from pathlib import Path
from unittest.mock import Mock

import pytest

from labstack import cluster, qemu
from labstack.inventory import nodes, read_config
from labstack.models.state import State
from labstack.settings import get_settings


class MonitorFake:
    """Serve QMP ownership queries over a real socket without starting a VM."""

    def __init__(self, path: Path, name: str):
        self.path = path
        self.name = name
        self.commands: list[str] = []
        self.socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.socket.settimeout(0.1)
        self.stopped = threading.Event()
        self.thread = threading.Thread(target=self.serve, daemon=True)

    def __enter__(self):
        self.socket.bind(str(self.path))
        self.socket.listen()
        self.thread.start()
        return self

    def serve(self):
        while not self.stopped.is_set():
            try:
                connection, _ = self.socket.accept()
            except TimeoutError:
                continue
            with connection, connection.makefile("rwb", buffering=0) as stream:
                stream.write(b'{"QMP": {}}\n')
                for _ in range(2):
                    request = json.loads(stream.readline())["execute"]
                    self.commands.append(request)
                    result = {"name": self.name} if request == "query-name" else {}
                    stream.write(json.dumps({"return": result}).encode() + b"\n")
                    if request == "system_powerdown":
                        self.path.unlink(missing_ok=True)

    def __exit__(self, *args):
        self.stopped.set()
        self.thread.join(timeout=2)
        self.socket.close()
        self.path.unlink(missing_ok=True)


@pytest.fixture
def legacy_root():
    """Keep old socket paths within macOS limits, independent of pytest's path."""
    with tempfile.TemporaryDirectory(prefix="ls-", dir="/tmp") as directory:
        yield Path(directory)


@pytest.fixture
def legacy_environment(legacy_root, monkeypatch):
    """Keep real legacy files and state, with every process left untouched."""
    settings = get_settings()
    config = read_config()
    (legacy_root / "machines.yml").write_text(config.model_dump_json())
    monkeypatch.setattr(settings, "root", legacy_root)
    monkeypatch.setattr(settings, "state_dir", legacy_root / ".state")
    monkeypatch.setattr(settings, "logs_dir", legacy_root / "logs")
    state = State(
        config=config,
        instance_id="saved",
        base_images=dict.fromkeys(("cp", "w1"), legacy_root / "base.qcow2"),
    )
    for node in nodes(state.config):
        directory = settings.state_dir / node.name
        directory.mkdir(parents=True)
        (directory / "disk.qcow2").write_bytes(b"guest data to preserve")
        (directory / "vars.fd").write_bytes(b"guest firmware state")
    cluster.save_state(state)
    yield state
    if settings.runtime_dir.is_dir():
        shutil.rmtree(settings.runtime_dir)


def test_legacy_monitor_still_proves_guest_ownership(legacy_environment):
    state = legacy_environment
    node = nodes(state.config)[0]
    path = get_settings().state_dir / node.name / "qmp.sock"

    with MonitorFake(path, f"labstack-{state.instance_id}-{node.name}"):
        assert qemu.running(node, state) is True


def test_down_can_stop_owned_legacy_guests(legacy_environment):
    state = legacy_environment
    node = nodes(state.config)[0]
    path = get_settings().state_dir / node.name / "qmp.sock"

    with MonitorFake(path, f"labstack-{state.instance_id}-{node.name}") as guest:
        cluster.down(state)

    assert "system_powerdown" in guest.commands
    assert qemu.running(node, state) is False
    assert (get_settings().state_dir / "cp" / "disk.qcow2").exists()


def test_legacy_live_pid_without_monitor_prevents_destroy(legacy_environment):
    state = legacy_environment
    directory = get_settings().state_dir / "cp"
    disk_contents = (directory / "disk.qcow2").read_bytes()
    # This PID only receives signal 0 to check existence. No process is stopped.
    (directory / "qemu.pid").write_text(str(os.getpid()))

    with pytest.raises(ValueError, match="live PID"):
        cluster.destroy(state)

    assert (directory / "disk.qcow2").read_bytes() == disk_contents
    assert cluster.read_state() == state


def test_active_legacy_guests_cannot_be_migrated(legacy_environment, monkeypatch):
    state = legacy_environment
    settings = get_settings()
    node = nodes(state.config)[0]
    monkeypatch.setattr(cluster, "requirements", Mock(return_value=settings.root))
    monkeypatch.setattr(cluster, "validate_images", Mock())

    with (
        MonitorFake(
            settings.state_dir / "cp" / "qmp.sock",
            f"labstack-{state.instance_id}-{node.name}",
        ),
        pytest.raises(ValueError, match="task down"),
    ):
        cluster.up(None, None, no_provision=True)

    assert (
        settings.state_dir / "cp" / "disk.qcow2"
    ).read_bytes() == b"guest data to preserve"
    assert not settings.machine_dir("cp").exists()


@pytest.mark.parametrize("name", ["cp", "shared", "machines"])
def test_stopped_legacy_resources_move_without_losing_guest_data(
    legacy_environment, name
):
    state = legacy_environment
    settings = get_settings()
    if name != "cp":
        definition = state.config.model_dump()
        definition["machines"] = {name: definition["machines"]["cp"]}
        definition["machines"][name]["scripts"] = []
        definition["machines"][name]["healthcheck"] = None
        definition["machines"][name]["depends_on"] = {}
        state = State(
            config=definition,
            instance_id="saved",
            base_images={name: settings.root / "base.qcow2"},
        )
        directory = settings.state_dir / name
        directory.mkdir(exist_ok=True)
        (directory / "disk.qcow2").write_bytes(b"guest data to preserve")
        (directory / "vars.fd").write_bytes(b"guest firmware state")
    shared = settings.state_dir / "shared"
    shared.mkdir(exist_ok=True)
    (shared / "join-command").write_text("recipe output")

    qemu.migrate_resources(state)

    assert (
        settings.machine_dir(name) / "disk.qcow2"
    ).read_bytes() == b"guest data to preserve"
    assert (
        settings.machine_dir(name) / "vars.fd"
    ).read_bytes() == b"guest firmware state"
    assert not (settings.state_dir / name / "disk.qcow2").exists()
    assert (shared / "join-command").read_text() == "recipe output"


@pytest.mark.parametrize("action", ["migrate", "destroy"])
def test_ambiguous_disks_are_kept_and_mutation_is_refused(legacy_environment, action):
    state = legacy_environment
    settings = get_settings()
    settings.machine_dir("cp").mkdir(parents=True)
    current_disk = settings.machine_dir("cp") / "disk.qcow2"
    current_disk.write_bytes(b"another disk")

    with pytest.raises(ValueError, match="both"):
        if action == "migrate":
            qemu.migrate_resources(state)
        else:
            cluster.destroy(state)

    assert current_disk.read_bytes() == b"another disk"
    assert (
        settings.state_dir / "cp" / "disk.qcow2"
    ).read_bytes() == b"guest data to preserve"


@pytest.mark.parametrize("order", [("seed", "machines"), ("machines", "seed")])
def test_migration_keeps_resources_when_old_seed_overlaps_a_new_machine(
    legacy_environment, order
):
    """The legacy machines/seed directory can become the new seed VM directory."""
    settings = get_settings()
    definition = legacy_environment.config.model_dump()
    original = list(definition["machines"].values())[:2]
    definition["machines"] = dict(zip(order, original, strict=True))
    for machine in definition["machines"].values():
        machine.update(scripts=[], healthcheck=None, depends_on={})
    state = State(
        config=definition,
        instance_id="saved",
        base_images=dict.fromkeys(order, settings.root / "base.qcow2"),
    )
    for name in order:
        directory = settings.state_dir / name
        (directory / "seed").mkdir(parents=True)
        (directory / "disk.qcow2").write_bytes(f"guest data: {name}".encode())
        (directory / "vars.fd").write_bytes(f"firmware: {name}".encode())
        (directory / "seed" / "user-data").write_text(f"cloud-init: {name}")

    qemu.migrate_resources(state)
    qemu.migrate_resources(state)

    for name in order:
        directory = settings.machine_dir(name)
        assert (directory / "disk.qcow2").read_bytes() == f"guest data: {name}".encode()
        assert (directory / "vars.fd").read_bytes() == f"firmware: {name}".encode()
        assert (directory / "seed" / "user-data").read_text() == f"cloud-init: {name}"


def test_recipe_output_named_disk_does_not_block_a_new_shared_vm(legacy_environment):
    settings = get_settings()
    definition = legacy_environment.config.model_dump()
    machine = definition["machines"]["cp"]
    machine.update(scripts=[], healthcheck=None, depends_on={})
    definition["machines"] = {"shared": machine}
    state = State(
        config=definition,
        instance_id="saved",
        base_images={"shared": settings.root / "base.qcow2"},
        resource_layout="namespaced",
    )
    node = nodes(state.config)[0]
    settings.machine_dir("shared").mkdir(parents=True)
    disk = settings.machine_dir("shared") / "disk.qcow2"
    disk.write_bytes(b"guest data")
    shared = settings.state_dir / "shared"
    shared.mkdir()
    (shared / "disk.qcow2").write_bytes(b"recipe output")
    qemu.prepare_runtime_directory()

    with MonitorFake(
        settings.runtime_dir / "qmp-0.sock", f"labstack-{state.instance_id}-{node.name}"
    ):
        assert qemu.running(node, state) is True
        cluster.down(state)

    assert disk.read_bytes() == b"guest data"
    assert (shared / "disk.qcow2").read_bytes() == b"recipe output"
    cluster.destroy(state)
    assert not settings.state_dir.exists()
