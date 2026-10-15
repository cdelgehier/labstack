"""Prepare isolated cloud-init media and writable resources for each guest."""

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

from labstack.inventory import nodes
from labstack.models.machine import Node
from labstack.models.state import State
from labstack.settings import get_settings


def cloud_config(node: Node, state: State, public_key: str) -> dict[str, Any]:
    """Prepare first-boot access and identity without installing a workload.

    Each environment has its own SSH key. The factory's public Vagrant key is replaced,
    password access stays disabled, and sudo belongs to the selected SSH account.
    Hostnames, host entries and disk growth apply in both normal and no-provision mode;
    software installation belongs to the inventory's script lists.
    """
    hosts = "127.0.0.1 localhost\n::1 localhost ip6-localhost\n" + "".join(
        f"{machine.address} {machine.name}\n" for machine in nodes(state.config)
    )
    return {
        "hostname": node.name,
        "manage_etc_hosts": False,
        "users": [
            {
                "name": node.ssh_user,
                "homedir": f"/home/{node.ssh_user}",
                "groups": ["sudo"],
                "shell": "/bin/bash",
                "sudo": "ALL=(ALL) NOPASSWD:ALL",
                "lock_passwd": True,
                "ssh_authorized_keys": [public_key],
            }
        ],
        "disable_root": True,
        "ssh_pwauth": False,
        "ssh_deletekeys": True,
        "growpart": {
            "mode": "auto",
            "devices": ["/"],
            "ignore_growroot_disabled": True,
        },
        "resize_rootfs": True,
        "write_files": [
            {"path": "/etc/hosts", "permissions": "0644", "content": hosts},
            # The factory image contains a public box key. Replace it for direct QEMU use.
            {
                "path": f"/home/{node.ssh_user}/.ssh/authorized_keys",
                "permissions": "0600",
                "owner": f"{node.ssh_user}:{node.ssh_user}",
                "defer": True,
                "content": public_key + "\n",
            },
        ],
        "runcmd": [["systemctl", "restart", "ssh"]],
    }


def network_config(node: Node) -> dict[str, Any]:
    """Keep Internet routing on NAT and guest traffic on the private interface.

    MAC matching makes the configuration independent of Ubuntu's device numbering.
    The NAT interface keeps its existing name because initramfs may already be running
    DHCP on it. Only the private interface is renamed and receives a static address,
    without a second default route.
    """
    return {
        "version": 2,
        "ethernets": {
            # Ubuntu starts DHCP in initramfs. Renaming this active NIC would fail.
            "nat0": {
                "match": {"macaddress": node.nat_mac},
                "dhcp4": True,
            },
            "cluster0": {
                "match": {"macaddress": node.cluster_mac},
                "set-name": "cluster0",
                "dhcp4": False,
                "addresses": [str(node.private_ip)],
            },
        },
    }


def prepare_node(node: Node, state: State, firmware: Path) -> None:
    """Create missing guest resources while preserving an existing writable disk.

    Each VM has an overlay, its own UEFI variables and its own cloud-init seed. The
    requested capacity must fit the complete base disk. Rebuilding the seed on restart
    keeps the saved instance ID, so cloud-init does not treat that restart as a new VM.
    A reset supplies a new instance ID and removes the old overlay first.
    """
    settings = get_settings()
    directory = settings.machine_dir(node.name)
    directory.mkdir(parents=True, exist_ok=True)
    disk = directory / "disk.qcow2"
    if not disk.exists():
        result = subprocess.run(
            ["qemu-img", "info", "--output=json", str(state.base_images[node.name])],
            check=True,
            capture_output=True,
            text=True,
        )
        # A smaller overlay would hide the end of the base disk, including its partitions.
        if node.disk_gib * 1024**3 < json.loads(result.stdout)["virtual-size"]:
            raise ValueError(f"{node.name}: disk_gib is smaller than the base image.")
        subprocess.run(
            [
                "qemu-img",
                "create",
                "-f",
                "qcow2",
                "-F",
                "qcow2",
                "-b",
                str(state.base_images[node.name]),
                str(disk),
                f"{node.disk_gib}G",
            ],
            check=True,
        )
    if not (directory / "vars.fd").exists():
        # Firmware code is shared, but UEFI variables contain writable per-guest boot state.
        shutil.copyfile(firmware / "edk2-arm-vars.fd", directory / "vars.fd")
    seed = directory / "seed"
    seed.mkdir(exist_ok=True)
    public_key = (settings.state_dir / "id_ed25519.pub").read_text().strip()
    (seed / "user-data").write_text(
        "#cloud-config\n" + json.dumps(cloud_config(node, state, public_key))
    )
    (seed / "meta-data").write_text(
        json.dumps(
            {
                # A stable ID prevents cloud-init from resetting access and host identity
                # on each restart. Only a recreated environment receives a new ID.
                "instance-id": f"{state.instance_id}-{node.name}",
                "local-hostname": node.name,
            }
        )
    )
    (seed / "network-config").write_text(json.dumps(network_config(node)))
    subprocess.run(
        [
            "xorriso",
            "-as",
            "mkisofs",
            "-quiet",
            "-output",
            str(directory / "seed.iso"),
            "-volid",
            "cidata",
            "-joliet",
            "-rock",
            str(seed),
        ],
        check=True,
    )
