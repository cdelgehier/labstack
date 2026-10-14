import json
import shutil
import subprocess
from unittest.mock import Mock

import pytest
from pydantic import ValidationError

from labstack.cloud_init import cloud_config, network_config
from labstack.images import verify_sha256
from labstack.inventory import nodes, read_config, resolve_config
from labstack.models.state import State


def test_default_cluster_has_one_control_plane_and_one_worker():
    config = resolve_config(None, None)
    machines = nodes(config)

    assert [node.name for node in machines] == ["cp", "w1"]
    assert [node.address for node in machines] == ["192.168.76.10", "192.168.76.11"]


def test_second_worker_has_its_own_network_identity():
    machines = nodes(resolve_config(None, ("extra-worker",)))

    assert len(machines) == 3
    assert len({node.ssh_port for node in machines}) == 3
    assert len({node.cluster_mac for node in machines}) == 3
    assert machines[2].address == "192.168.76.12"


@pytest.mark.parametrize("profiles", [("missing",), ("../extra-worker",)])
def test_invalid_profile_selection_is_rejected(profiles):
    with pytest.raises(ValidationError):
        resolve_config(None, profiles)


def test_resume_keeps_the_existing_profiles():
    existing = resolve_config(None, ("extra-worker",))

    assert resolve_config(existing, None) == existing


def test_resume_refuses_to_change_the_existing_profiles():
    with pytest.raises(ValueError, match="task destroy"):
        resolve_config(resolve_config(None, ("extra-worker",)), ())


def test_infra_cloud_init_sets_access_without_installing_kubernetes(tmp_path):
    state = State(
        config=resolve_config(None, None),
        instance_id="test-instance",
        base_images={"cp": tmp_path / "base", "w1": tmp_path / "base"},
    )
    cp, worker = nodes(state.config)

    cp_data = cloud_config(cp, state, "ssh-ed25519 TEST project")
    worker_data = cloud_config(worker, state, "ssh-ed25519 TEST project")

    assert cp_data["users"][0]["name"] == "ubuntu"
    assert "ssh-rsa" not in json.dumps(cp_data)
    assert cp_data["ssh_pwauth"] is False
    assert cp_data["runcmd"] == worker_data["runcmd"]
    assert "kubeadm" not in json.dumps(cp_data)
    assert "containerd" not in json.dumps(cp_data)


def test_only_the_nat_interface_has_a_default_route():
    config = network_config(nodes(read_config())[0])
    interfaces = config["ethernets"]

    assert interfaces["nat0"]["dhcp4"] is True
    assert interfaces["cluster0"]["addresses"] == ["192.168.76.10/24"]
    assert interfaces["cluster0"]["dhcp4"] is False
    assert "routes" not in interfaces["cluster0"]


def test_nat_interface_keeps_its_name_when_initramfs_has_already_started_dhcp():
    interfaces = network_config(nodes(read_config())[0])["ethernets"]

    assert "set-name" not in interfaces["nat0"]
    assert interfaces["nat0"]["match"]["macaddress"] == "52:54:00:76:00:00"


def test_qemu_forwards_ports_on_localhost_only(tmp_path):
    from labstack.qemu import qemu_command

    state = State(
        config=resolve_config(None, ("extra-worker",)),
        instance_id="test",
        base_images=dict.fromkeys(("cp", "w1", "w2"), tmp_path / "base"),
    )
    command = qemu_command(nodes(state.config)[0], state, tmp_path)

    assert (
        "user,id=nat,hostfwd=tcp:127.0.0.1:52222-:22,hostfwd=tcp:127.0.0.1:16443-:6443"
        in command
    )
    assert "hubport,id=port1,hubid=0,netdev=link1" in command
    assert "hubport,id=port2,hubid=0,netdev=link2" in command


def test_shutdown_rejects_a_monitor_owned_by_another_vm(tmp_path, monkeypatch):
    from labstack import (
        cluster,
        qemu,
    )

    state = State(
        config=read_config(),
        instance_id="test",
        base_images={"cp": tmp_path / "base", "w1": tmp_path / "base"},
    )
    monitor_mock = Mock(return_value={"name": "unrelated-vm"})
    monkeypatch.setattr(qemu, "qmp", monitor_mock)

    with pytest.raises(ValueError, match="another VM"):
        cluster.down(state)

    monitor_mock.assert_called_once()
    assert monitor_mock.call_args.args[1] == "query-name"


def test_checksum_mismatch_is_rejected(tmp_path):
    image = tmp_path / "image"
    image.write_bytes(b"corrupt download")

    with pytest.raises(ValueError, match="SHA-256"):
        verify_sha256(image, "0" * 64)


def test_checksum_accepts_the_expected_file(tmp_path):
    image = tmp_path / "image"
    image.write_bytes(b"abc")

    verify_sha256(
        image, "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
    )


@pytest.mark.skipif(not shutil.which("qemu-img"), reason="QEMU is not installed")
def test_base_disk_validation_rejects_backing_files(tmp_path):
    from labstack.images import validate_disk

    base = tmp_path / "base.qcow2"
    overlay = tmp_path / "overlay.qcow2"
    subprocess.run(["qemu-img", "create", "-f", "qcow2", str(base), "64M"], check=True)
    subprocess.run(
        [
            "qemu-img",
            "create",
            "-f",
            "qcow2",
            "-F",
            "qcow2",
            "-b",
            str(base),
            str(overlay),
        ],
        check=True,
    )

    validate_disk(base)
    with pytest.raises(ValueError, match="standalone QCOW2"):
        validate_disk(overlay)


@pytest.mark.skipif(not shutil.which("qemu-img"), reason="QEMU is not installed")
def test_local_image_is_copied_so_original_writes_cannot_change_the_base(
    tmp_path, monkeypatch
):
    from labstack import (
        images,
    )
    from labstack.settings import get_settings

    source = tmp_path / "source.qcow2"
    monkeypatch.setattr(get_settings(), "cache_dir", tmp_path / "cache")
    subprocess.run(
        ["qemu-img", "create", "-f", "qcow2", str(source), "64M"], check=True
    )

    cached = images.acquire_image(None, source)
    contents = cached.read_bytes()
    source.write_bytes(b"replaced image")

    assert cached.read_bytes() == contents
    assert cached.stat().st_mode & 0o222 == 0


@pytest.mark.skipif(not shutil.which("qemu-img"), reason="QEMU is not installed")
def test_reset_reuses_the_cached_base_without_a_second_copy(tmp_path, monkeypatch):
    from labstack import (
        images,
    )
    from labstack.settings import get_settings

    monkeypatch.setattr(get_settings(), "cache_dir", tmp_path)
    source = tmp_path / "ubuntu.qcow2"
    subprocess.run(
        ["qemu-img", "create", "-f", "qcow2", str(source), "64M"], check=True
    )

    assert images.acquire_image(None, source) == source.resolve()
