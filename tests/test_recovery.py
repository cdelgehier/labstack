"""Keep disks and saved image choices recoverable after lifecycle failures."""

import hashlib
import os
import shutil
import subprocess
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest
import yaml

from labstack import cluster, main
from labstack.inventory import read_config
from labstack.models.state import State
from labstack.settings import get_settings


class GuestStartStub:
    """Create access files without starting a guest or contacting SSH."""

    def __init__(self, fail_keygen=False):
        self.fail_keygen = fail_keygen
        self.run = subprocess.run

    def __call__(self, command, **kwargs):
        if command[0] not in {"ssh-keygen", "qemu-system-aarch64"}:
            return self.run(command, **kwargs)
        if command[0] == "ssh-keygen":
            if self.fail_keygen:
                raise OSError("Access key creation failed")
            key = Path(command[command.index("-f") + 1])
            key.write_text("private test key")
            key.with_suffix(".pub").write_text("ssh-ed25519 TEST")
        return subprocess.CompletedProcess(command, 0)


@pytest.fixture
def saved_environment(tmp_path, monkeypatch, published_box):
    """Use real inventory, image checksums and files around fake guest boundaries."""
    settings = get_settings()
    original_root = settings.root
    config = read_config()
    for machine in config.active_machines.values():
        for filename in machine.scripts:
            destination = tmp_path / filename
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(original_root / filename, destination)
    (tmp_path / "machines.yml").write_text(
        yaml.safe_dump(config.model_dump(mode="json"), sort_keys=False)
    )
    state_dir = tmp_path / ".state"
    state_dir.mkdir()
    disk = state_dir / "machines" / "cp" / "disk.qcow2"
    disk.parent.mkdir(parents=True)
    disk.write_bytes(b"guest work must survive a failed preflight")
    base = tmp_path / "base.qcow2"
    base.write_bytes(b"checked base image")
    pin = published_box.model_copy(
        update={"disk_sha256": hashlib.sha256(base.read_bytes()).hexdigest()}
    )
    state = State(
        config=config,
        instance_id="original",
        base_images=dict.fromkeys(config.active_machines, base),
        resolved_boxes=dict.fromkeys(config.active_machines, pin),
        provisioned=True,
    )
    (state_dir / "state.json").write_text(state.model_dump_json(indent=2))
    (state_dir / "id_ed25519").write_text("private test key")
    monkeypatch.setattr(settings, "root", tmp_path)
    monkeypatch.setattr(settings, "state_dir", state_dir)
    monkeypatch.setattr(settings, "logs_dir", tmp_path / "logs")
    monkeypatch.setattr(cluster, "requirements", Mock(return_value=tmp_path))
    monkeypatch.setattr(cluster, "validate_disk", Mock(return_value=64 * 1024**2))
    monkeypatch.setattr(cluster, "acquire_image", Mock(return_value=base))
    monkeypatch.setattr(cluster, "running", Mock(return_value=False))
    monkeypatch.setattr(cluster, "wait_for_guest", Mock())
    monkeypatch.setattr(cluster, "prepare_node", Mock())
    monkeypatch.setattr(cluster, "check_ports", Mock())
    monkeypatch.setattr(cluster, "provision", Mock())
    monkeypatch.setattr(cluster, "export_kubeconfig", Mock())
    monkeypatch.setattr(cluster, "prepare_client", Mock())
    monkeypatch.setattr(cluster.subprocess, "run", GuestStartStub())
    yield state, disk
    runtime = getattr(settings, "runtime_dir", None)
    if runtime is not None and runtime.is_dir():
        shutil.rmtree(runtime)


@pytest.mark.parametrize(
    "failure", ["script", "firmware", "checksum", "capacity", "ports"]
)
def test_reset_keeps_the_environment_when_preflight_fails(
    saved_environment, monkeypatch, failure
):
    state, disk = saved_environment
    state_file = get_settings().state_dir / "state.json"
    original_state = state_file.read_bytes()
    original_disk = disk.read_bytes()
    if failure == "script":
        (get_settings().root / state.config.machines["cp"].scripts[0]).unlink()
    elif failure == "firmware":
        monkeypatch.setattr(
            cluster, "requirements", Mock(side_effect=ValueError("Missing firmware"))
        )
    elif failure == "checksum":
        state.base_images["cp"].write_bytes(b"changed image")
    elif failure == "capacity":
        monkeypatch.setattr(cluster, "validate_disk", Mock(return_value=64 * 1024**3))
    else:
        monkeypatch.setattr(
            cluster, "check_ports", Mock(side_effect=ValueError("Host port is in use"))
        )
    monkeypatch.setattr(sys, "argv", ["labstack", "reset"])

    with pytest.raises(ValueError):
        main.main()

    assert disk.read_bytes() == original_disk
    assert state_file.read_bytes() == original_state


def test_reset_can_resume_after_failure_before_new_state_exists(
    saved_environment, monkeypatch
):
    original, _ = saved_environment
    monkeypatch.setattr(sys, "argv", ["labstack", "reset"])
    monkeypatch.setattr(cluster.subprocess, "run", GuestStartStub(fail_keygen=True))

    with pytest.raises(OSError, match="Access key"):
        main.main()
    assert not (get_settings().state_dir / "state.json").exists()
    assert get_settings().reset_file.stat().st_mode & 0o777 == 0o600

    monkeypatch.setattr(cluster.subprocess, "run", GuestStartStub())
    main.main()

    recreated = cluster.read_state()
    assert recreated.instance_id != original.instance_id
    assert recreated.config == original.config
    assert recreated.resolved_boxes == original.resolved_boxes
    assert recreated.provisioned is True
    assert not (get_settings().root / ".reset.json").exists()


def test_reset_reuses_environment_without_reading_source_files(saved_environment):
    original, _ = saved_environment
    definition = original.config.model_dump()
    definition["machines"]["cp"]["env_file"] = ["deleted.env"]
    saved = State.model_validate(
        {
            **original.model_dump(),
            "config": definition,
            "environments": {"cp": {"LAB_NAME": "saved"}, "w1": {}},
        }
    )
    cluster.save_state(saved)

    cluster.reset()

    recreated = cluster.read_state()
    assert recreated.environments == saved.environments
    assert recreated.environment_for("cp") == {"LAB_NAME": "saved"}
    assert recreated.instance_id != saved.instance_id


def test_ordinary_up_cannot_bypass_a_pending_reset(saved_environment, monkeypatch):
    original, disk = saved_environment
    settings = get_settings()
    cluster.save_state(original, path=settings.reset_file)
    (settings.state_dir / "state.json").unlink()
    journal = settings.reset_file.read_bytes()
    disk_contents = disk.read_bytes()
    resolver = Mock(side_effect=AssertionError("A reset must keep its saved pins"))
    monkeypatch.setattr(cluster, "resolve_boxes", resolver)

    with pytest.raises(ValueError, match="task reset"):
        cluster.up(None, None, no_provision=True)

    assert settings.reset_file.read_bytes() == journal
    assert disk.read_bytes() == disk_contents
    assert not (settings.state_dir / "state.json").exists()
    resolver.assert_not_called()


@pytest.mark.parametrize("remove_state_directory", [False, True])
def test_destroy_can_cancel_reset_before_new_state_exists(
    saved_environment, monkeypatch, remove_state_directory
):
    settings = get_settings()
    monkeypatch.setattr(sys, "argv", ["labstack", "reset"])
    monkeypatch.setattr(cluster.subprocess, "run", GuestStartStub(fail_keygen=True))
    with pytest.raises(OSError, match="Access key"):
        main.main()
    assert not (settings.state_dir / "state.json").exists()
    if remove_state_directory:
        shutil.rmtree(settings.state_dir)
    (settings.root / "kubeconfig").write_text("private access")
    settings.logs_dir.mkdir(exist_ok=True)
    log = settings.logs_dir / "last-error.log"
    log.write_text("useful failure details")
    monkeypatch.setattr(sys, "argv", ["labstack", "destroy"])

    main.main()

    assert not settings.state_dir.exists()
    assert not settings.reset_file.exists()
    assert not (settings.root / "kubeconfig").exists()
    assert log.read_text() == "useful failure details"


def test_successful_state_replacement_keeps_private_permissions(saved_environment):
    state, _ = saved_environment
    updated = state.model_copy(update={"provisioned": False})

    cluster.save_state(updated)

    assert cluster.read_state() == updated
    assert (get_settings().state_dir / "state.json").stat().st_mode & 0o777 == 0o600
    assert not list(get_settings().state_dir.glob(".state.json-*"))


def test_partial_state_write_keeps_the_previous_valid_state(saved_environment):
    """A child file-size limit models a short write without limiting the test runner."""
    settings = get_settings()
    original = (settings.state_dir / "state.json").read_bytes()
    code = """
import resource
import signal
from pathlib import Path
from unittest.mock import Mock
from labstack import cluster

cluster.requirements = Mock(return_value=Path('.'))
cluster.validate_disk = Mock(return_value=64 * 1024**2)
cluster.running = Mock(return_value=True)
cluster.wait_for_guest = Mock()
cluster.provision = Mock()
signal.signal(signal.SIGXFSZ, signal.SIG_IGN)
resource.setrlimit(resource.RLIMIT_FSIZE, (1024, 1024))
try:
    cluster.up(None, None)
except OSError:
    pass
else:
    raise AssertionError('Expected the state write to exceed the file size limit')
"""

    subprocess.run(
        [sys.executable, "-c", code],
        env=dict(
            os.environ, LABSTACK_ROOT=str(settings.root), PROFILES="", IMAGE_PATH=""
        ),
        check=True,
        capture_output=True,
        text=True,
    )

    assert (settings.state_dir / "state.json").read_bytes() == original
    assert cluster.read_state().instance_id == "original"
