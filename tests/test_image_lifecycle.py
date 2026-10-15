"""Check image selection across creation, restart and reset."""

import shutil
import subprocess
from unittest.mock import Mock

import pytest
from pydantic import ValidationError

from labstack import (
    cluster,
    images,
)
from labstack.images import verify_sha256
from labstack.models.image import BoxReference
from labstack.models.state import State
from labstack.settings import get_settings


@pytest.fixture
def guest_environment(tmp_path, monkeypatch, published_box):
    """Keep saved state real while replacing QEMU and SSH boundaries."""
    directory = tmp_path / "state"
    directory.mkdir()
    (directory / "id_ed25519").write_text("test key")
    monkeypatch.setattr(get_settings(), "state_dir", directory)
    monkeypatch.setattr(cluster, "requirements", Mock(return_value=tmp_path))
    monkeypatch.setattr(cluster, "acquire_image", Mock(return_value=tmp_path / "base"))
    monkeypatch.setattr(cluster, "validate_disk", Mock(return_value=64 * 1024**2))
    monkeypatch.setattr(cluster, "verify_sha256", Mock())
    monkeypatch.setattr(cluster, "running", Mock(return_value=True))
    monkeypatch.setattr(cluster, "wait_for_guest", Mock())
    resolver = Mock(return_value=published_box)
    monkeypatch.setattr(images, "resolve_box", resolver)
    return resolver


def test_shared_box_is_resolved_once_and_pinned_for_every_machine(guest_environment):
    cluster.up(("extra-worker",), None, no_provision=True)

    saved = cluster.read_state()
    assert set(saved.resolved_boxes) == {"cp", "w1", "w2"}
    assert {box.release for box in saved.resolved_boxes.values()} == {"v0.4.0"}
    assert saved.config.machines["cp"].box == BoxReference(target="ubuntu-26.04-arm64")
    guest_environment.assert_called_once()


def test_shared_base_is_acquired_once_for_all_clones(guest_environment, monkeypatch):
    acquired = Mock(return_value=get_settings().state_dir.parent / "base")
    monkeypatch.setattr(cluster, "acquire_image", acquired)

    cluster.up(("extra-worker",), None, no_provision=True)

    assert len(set(cluster.read_state().base_images.values())) == 1
    acquired.assert_called_once()


def test_restart_keeps_the_pin_without_reading_the_catalogue(guest_environment):
    cluster.up(None, None, no_provision=True)
    original = cluster.read_state()
    guest_environment.side_effect = OSError("Catalogue is offline")

    cluster.up(None, None, no_provision=True)

    restarted = cluster.read_state()
    assert restarted.resolved_boxes == original.resolved_boxes
    assert restarted.instance_id == original.instance_id
    assert guest_environment.call_count == 1


def test_restart_checks_the_saved_checksum_before_starting_guests(
    guest_environment, monkeypatch
):
    cluster.up(None, None, no_provision=True)
    original = cluster.read_state()
    original.base_images["cp"].write_bytes(b"changed base image")
    monkeypatch.setattr(cluster, "verify_sha256", verify_sha256)

    with pytest.raises(ValueError, match="SHA-256"):
        cluster.up(None, None, no_provision=True)


@pytest.mark.parametrize("change", ["missing_machine", "target", "version", "account"])
def test_saved_pins_must_match_the_active_machine_references(guest_environment, change):
    cluster.up(None, None, no_provision=True)
    definition = cluster.read_state().model_dump()
    if change == "missing_machine":
        del definition["resolved_boxes"]["w1"]
    elif change == "target":
        definition["resolved_boxes"]["cp"]["target"] = "debian-13-arm64"
    elif change == "account":
        definition["resolved_boxes"]["cp"]["ssh_user"] = "debian"
    else:
        definition["config"]["machines"]["cp"]["box"]["version"] = "0.5.0"

    with pytest.raises(ValidationError, match="Saved box metadata"):
        State.model_validate(definition)


def test_reset_uses_saved_pins_and_base_paths_without_the_catalogue(
    guest_environment, monkeypatch
):
    cluster.up(None, None, no_provision=True)
    original = cluster.read_state()
    (get_settings().state_dir / "state.json").unlink()
    guest_environment.side_effect = OSError("Catalogue is offline")
    acquire_mock = Mock(return_value=original.base_images["cp"])
    monkeypatch.setattr(cluster, "acquire_image", acquire_mock)

    cluster.up(None, None, saved=original, no_provision=True)

    reset = cluster.read_state()
    assert reset.resolved_boxes == original.resolved_boxes
    assert reset.base_images == original.base_images
    assert reset.instance_id != original.instance_id
    assert acquire_mock.call_args_list[0].args == (
        original.resolved_boxes["cp"],
        original.base_images["cp"],
    )
    assert guest_environment.call_count == 1


def test_local_disk_override_skips_remote_resolution(guest_environment, monkeypatch):
    guest_environment.side_effect = OSError("Catalogue is offline")
    acquire_mock = Mock(return_value=get_settings().state_dir.parent / "cached.qcow2")
    monkeypatch.setattr(cluster, "acquire_image", acquire_mock)
    local_disk = get_settings().state_dir.parent / "local.qcow2"

    cluster.up(None, local_disk, no_provision=True)

    assert cluster.read_state().resolved_boxes == {}
    assert acquire_mock.call_args_list[0].args == (None, local_disk)
    guest_environment.assert_not_called()


@pytest.mark.skipif(not shutil.which("qemu-img"), reason="QEMU is not installed")
def test_reset_rejects_a_cached_disk_that_no_longer_matches_its_pin(
    tmp_path, monkeypatch, published_box
):
    monkeypatch.setattr(get_settings(), "cache_dir", tmp_path)
    disk = tmp_path / "cached.qcow2"
    subprocess.run(["qemu-img", "create", "-f", "qcow2", str(disk), "64M"], check=True)

    with pytest.raises(ValueError, match="SHA-256"):
        images.acquire_image(published_box, disk)
