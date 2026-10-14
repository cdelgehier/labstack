import hashlib
import json
import shutil
import subprocess

import pytest
import yaml
from pydantic import ValidationError

from labstack import (
    cloud_init,
    images,
    inventory,
)
from labstack.inventory import nodes, read_config, select_node
from labstack.models.config import Config
from labstack.models.image import Box
from labstack.models.state import State
from labstack.settings import get_settings


def test_yaml_names_and_shared_box_are_used_for_the_nodes(tmp_path):
    inventory = tmp_path / "machines.yml"
    inventory.write_text(
        """boxes_catalogue: https://cdelgehier.github.io/image-factory/
machines:
  main:
    cpus: 4
    memory_mib: 4096
    networks:
      nat: {}
      lab:
        ipv4_address: 192.168.76.10
    ports: ["52222:22", "16443:6443"]
    box: &ubuntu ubuntu-26.04-arm64:0.4.0
    scripts: &base
      - provision/base/packages.sh
  compute-a:
    networks:
      nat: {}
      lab:
        ipv4_address: 192.168.76.11
    ports: ["52232:22"]
    box: *ubuntu
    scripts: *base
networks:
  nat:
    driver: nat
  lab:
    driver: socket
    subnet: 192.168.76.0/24
"""
    )

    config = read_config(inventory)
    cp, worker = nodes(config)

    assert [cp.name, worker.name] == ["main", "compute-a"]
    assert cp.box == worker.box
    assert cp.cpus == 4
    assert cp.memory_mib == 4096
    assert cp.scripts == worker.scripts == ("provision/base/packages.sh",)
    assert select_node(config, "main") == cp
    assert select_node(config, "compute-a") == worker


def test_roles_cannot_silently_select_provisioning():
    definition = read_config().model_dump()
    definition["machines"]["cp"]["role"] = "control-plane"

    with pytest.raises(ValidationError, match="Extra inputs"):
        Config.model_validate(definition)


@pytest.mark.parametrize(
    "url",
    [
        "http://cdelgehier.github.io/image-factory/",
        "https://cdelgehier.github.io/image-factory/?version=latest",
        "https://cdelgehier.github.io/image-factory/#ubuntu",
        "https://user:password@example.com/",
    ],
)
def test_catalogue_requires_an_https_base_url(url):
    definition = read_config().model_dump()
    definition["boxes_catalogue"] = url

    with pytest.raises(ValidationError, match="boxes_catalogue"):
        Config.model_validate(definition)


def test_reading_the_inventory_does_not_read_remote_metadata(monkeypatch):
    from unittest.mock import Mock

    resolver_mock = Mock(side_effect=OSError("Catalogue is offline"))
    monkeypatch.setattr(images, "resolve_box", resolver_mock)

    config = read_config()

    assert config.machines["cp"].box.target == "ubuntu-26.04-arm64"
    resolver_mock.assert_not_called()


def test_connection_selects_a_machine_key_even_when_it_was_a_positional_alias():
    definition = read_config().model_dump()
    definition["machines"] = {
        "worker-1": definition["machines"]["cp"],
        **{
            name: machine
            for name, machine in definition["machines"].items()
            if name != "cp"
        },
    }
    for machine in definition["machines"].values():
        machine["depends_on"] = {}
    config = Config.model_validate(definition)

    assert select_node(config, "worker-1").index == 0


@pytest.mark.parametrize("name", ["w2", "missing", "control-plane"])
def test_connection_rejects_unavailable_names_and_lists_active_machines(name):
    with pytest.raises(ValueError, match="Available machines: cp, w1"):
        select_node(read_config(), name)


def test_image_changes_cannot_reuse_existing_machine_disks(tmp_path, monkeypatch):
    existing = read_config()
    definition = existing.model_dump(mode="json")
    definition["machines"]["cp"]["box"]["version"] = "0.5.0"
    (tmp_path / "machines.yml").write_text(yaml.safe_dump(definition))
    monkeypatch.setattr(get_settings(), "root", tmp_path)

    with pytest.raises(ValueError, match="task destroy"):
        inventory.resolve_config(existing, None)


@pytest.mark.parametrize(
    "field,value", [("box_sha256", "bad"), ("url", "http://example.com/ubuntu.box")]
)
def test_invalid_box_metadata_is_rejected_before_download(field, value, published_box):
    definition = published_box.model_dump()
    definition[field] = value

    with pytest.raises(ValidationError):
        Box.model_validate(definition)


@pytest.mark.skipif(not shutil.which("qemu-img"), reason="QEMU is not installed")
def test_different_disks_from_the_same_release_have_separate_cache_entries(
    tmp_path, monkeypatch, published_box
):
    monkeypatch.setattr(get_settings(), "cache_dir", tmp_path)
    boxes = []
    for size in ("64M", "128M"):
        disk = tmp_path / f"{size}.qcow2"
        subprocess.run(
            ["qemu-img", "create", "-f", "qcow2", str(disk), size], check=True
        )
        digest = hashlib.sha256(disk.read_bytes()).hexdigest()
        disk.rename(tmp_path / f"base-{digest}.qcow2")
        definition = published_box.model_dump()
        definition["disk_sha256"] = digest
        boxes.append(Box.model_validate(definition))

    first = images.acquire_image(boxes[0])
    second = images.acquire_image(boxes[1])

    assert first != second
    assert first.exists() and second.exists()


@pytest.mark.skipif(
    not shutil.which("qemu-img") or not shutil.which("xorriso"),
    reason="QEMU tools are missing",
)
def test_each_overlay_uses_its_assigned_base_and_disk_capacity(tmp_path, monkeypatch):
    definition = read_config().model_dump()
    definition["machines"]["cp"]["disk_gib"] = 16
    definition["machines"]["w1"]["disk_gib"] = 20
    config = Config.model_validate(definition)
    directory = tmp_path / "state"
    directory.mkdir()
    monkeypatch.setattr(get_settings(), "state_dir", directory)
    (directory / "id_ed25519.pub").write_text("ssh-ed25519 TEST project")
    firmware = tmp_path / "firmware"
    firmware.mkdir()
    (firmware / "edk2-arm-vars.fd").write_bytes(b"test variables")
    bases = {}
    for node in nodes(config):
        base = tmp_path / f"{node.name}.qcow2"
        subprocess.run(
            ["qemu-img", "create", "-f", "qcow2", str(base), "64M"], check=True
        )
        bases[node.name] = base
    state = State(config=config, instance_id="test", base_images=bases)

    for node in nodes(config):
        cloud_init.prepare_node(node, state, firmware)
        result = subprocess.run(
            [
                "qemu-img",
                "info",
                "--output=json",
                str(directory / "machines" / node.name / "disk.qcow2"),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        info = json.loads(result.stdout)
        assert info["backing-filename"] == str(bases[node.name])
        assert info["virtual-size"] == node.disk_gib * 1024**3


@pytest.mark.skipif(
    not shutil.which("qemu-img") or not shutil.which("xorriso"),
    reason="QEMU tools are missing",
)
def test_machine_disk_cannot_truncate_its_base_image(tmp_path, monkeypatch):
    definition = read_config().model_dump()
    definition["machines"]["cp"]["disk_gib"] = 16
    config = Config.model_validate(definition)
    directory = tmp_path / "state"
    directory.mkdir()
    monkeypatch.setattr(get_settings(), "state_dir", directory)
    (directory / "id_ed25519.pub").write_text("ssh-ed25519 TEST project")
    firmware = tmp_path / "firmware"
    firmware.mkdir()
    (firmware / "edk2-arm-vars.fd").write_bytes(b"test variables")
    base = tmp_path / "large-base.qcow2"
    subprocess.run(["qemu-img", "create", "-f", "qcow2", str(base), "24G"], check=True)
    state = State(
        config=config,
        instance_id="test",
        base_images={node.name: base for node in nodes(config)},
    )

    with pytest.raises(ValueError, match="smaller than the base image"):
        cloud_init.prepare_node(nodes(config)[0], state, firmware)

    assert not (directory / "machines" / "cp" / "disk.qcow2").exists()
