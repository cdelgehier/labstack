"""Build QEMU arguments and control only VMs owned by the saved environment."""

import json
import os
import platform
import shutil
import socket
import subprocess
from pathlib import Path
from typing import Any

from labstack.inventory import nodes
from labstack.models.machine import Node
from labstack.models.state import State
from labstack.settings import get_settings

MACHINE_RESOURCES = (
    "disk.qcow2",
    "vars.fd",
    "seed.iso",
    "seed/user-data",
    "seed/meta-data",
    "seed/network-config",
)


def resource_path(node: Node, name: str, state: State) -> Path:
    """Find an existing resource without overlooking an older running guest.

    Older labs kept resources directly under .state/<name>. Commands must still find
    their monitor and PID before startup or cleanup. New state identifies its layout,
    so shared recipe output cannot be mistaken for a VM. In an older record, two
    copies are ambiguous and must be inspected before the guest is changed.
    """
    settings = get_settings()
    legacy = settings.state_dir / node.name / name
    current = (
        settings.runtime_dir / f"qmp-{node.index}.sock"
        if name == "qmp.sock"
        else settings.machine_dir(node.name) / name
    )
    if state.resource_layout == "legacy" and (legacy.exists() or legacy.is_symlink()):
        if current.exists() or current.is_symlink():
            raise ValueError(
                f"{node.name}: {name} exists in both resource layouts. "
                "Inspect the files before restarting or removing this lab."
            )
        return legacy
    return current


def migrate_resources(state: State) -> State:
    """Move stopped guest resources while leaving shared recipe files in place.

    Check all guests and destination paths before moving anything. Move individual
    files so a VM named shared, seed or machines cannot move a directory containing
    another VM. A retry can finish a partial move, keeping disks and UEFI state.
    Return the new layout for atomic state saving before any guest starts.
    """
    if state.resource_layout == "namespaced":
        return state
    settings = get_settings()
    moves = []
    stale_files = []
    for node in nodes(state.config):
        for name in (*MACHINE_RESOURCES, "qemu.pid", "qmp.sock"):
            path = resource_path(node, name, state)
            if path != settings.state_dir / node.name / name or not path.exists():
                continue
            if name in MACHINE_RESOURCES:
                moves.append((path, settings.machine_dir(node.name) / name))
            else:
                stale_files.append(path)
    if not moves and not stale_files:
        return state.model_copy(update={"resource_layout": "namespaced"})
    for node in nodes(state.config):
        if running(node, state):
            raise ValueError(
                "Existing VM resources need to move. Run 'task down', then 'task up'."
            )
    for source, destination in moves:
        destination.parent.mkdir(parents=True, exist_ok=True)
        source.rename(destination)
    for path in stale_files:
        path.unlink()
    # Legacy network sockets are safe to remove only after every guest is stopped.
    for node in nodes(state.config)[1:]:
        (settings.state_dir / f"bus{node.index}.sock").unlink(missing_ok=True)
    return state.model_copy(update={"resource_layout": "namespaced"})


def qemu_value(path: Path) -> str:
    """Escape a filesystem path used inside a comma-separated QEMU option.

    QEMU splits drive and socket option values on commas. Doubling a comma preserves
    it as part of the path rather than letting it start another option.
    """
    return str(path).replace(",", ",,")


def qemu_command(node: Node, state: State, firmware: Path) -> list[str]:
    """Build an Apple Silicon guest command with isolated NAT and private links.

    HVF runs the ARM64 guest, UEFI boots the shared image, and only the overlay is
    writable. Published ports bind to localhost. The first guest owns a QEMU hub with
    one socket stream per worker, allowing worker-to-worker traffic as well as traffic
    to the first machine. Private monitor paths stay short enough for macOS sockets.
    """
    settings = get_settings()
    directory = settings.machine_dir(node.name)
    # Host forwards use NAT. The separate socket link carries only private guest traffic.
    forwards = ",".join(
        f"hostfwd=tcp:127.0.0.1:{port.published}-:{port.target}" for port in node.ports
    )
    command = [
        "qemu-system-aarch64",
        "-name",
        f"labstack-{state.instance_id}-{node.name}",
        "-machine",
        "virt,highmem=on,accel=hvf",
        "-cpu",
        "host",
        "-smp",
        str(node.cpus),
        "-m",
        str(node.memory_mib),
        "-drive",
        f"if=pflash,format=raw,readonly=on,file={qemu_value(firmware / 'edk2-aarch64-code.fd')}",
        "-drive",
        f"if=pflash,format=raw,file={qemu_value(directory / 'vars.fd')}",
        "-drive",
        f"if=virtio,format=qcow2,file={qemu_value(directory / 'disk.qcow2')},discard=unmap",
        "-drive",
        f"file={qemu_value(directory / 'seed.iso')},format=raw,media=cdrom",
        "-netdev",
        f"user,id=nat,{forwards}",
        "-device",
        f"virtio-net-device,netdev=nat,mac={node.nat_mac}",
        "-device",
        f"virtio-net-device,netdev=cluster,mac={node.cluster_mac}",
        "-qmp",
        f"unix:{qemu_value(settings.runtime_dir / f'qmp-{node.index}.sock')},server=on,wait=off",
        "-pidfile",
        str(directory / "qemu.pid"),
        "-serial",
        f"file:{qemu_value(settings.logs_dir / (node.name + '-serial.log'))}",
        "-display",
        "none",
        "-monitor",
        "none",
        "-parallel",
        "none",
        "-daemonize",
    ]
    if node.name == nodes(state.config)[0].name:
        command += ["-netdev", "hubport,id=cluster,hubid=0"]
        # Each stream is one worker link. The hub also forwards worker-to-worker traffic.
        for worker in nodes(state.config)[1:]:
            command += [
                "-netdev",
                f"stream,id=link{worker.index},server=on,addr.type=unix,"
                f"addr.path={qemu_value(settings.runtime_dir / f'bus{worker.index}.sock')}",
                "-netdev",
                f"hubport,id=port{worker.index},hubid=0,netdev=link{worker.index}",
            ]
    else:
        command += [
            "-netdev",
            "stream,id=cluster,server=off,addr.type=unix,"
            f"addr.path={qemu_value(settings.runtime_dir / f'bus{node.index}.sock')},reconnect-ms=1000",
        ]
    return command


def qmp(node: Node, command: str, state: State) -> Any:
    """Send one QEMU monitor command and wait for its actual response.

    The connection first enables QMP capabilities. Monitor events can arrive between
    responses and are ignored until a return or error appears. The caller must check
    VM ownership before using a command that shuts down or removes resources.
    """
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(5)
        connection.connect(str(resource_path(node, "qmp.sock", state)))
        with connection.makefile("rwb", buffering=0) as stream:
            json.loads(stream.readline())
            for request in ("qmp_capabilities", command):
                stream.write(json.dumps({"execute": request}).encode() + b"\n")
                while True:
                    # QMP can send asynchronous events before the command response.
                    line = stream.readline()
                    if not line:
                        raise OSError("QEMU closed its monitor connection.")
                    response = json.loads(line)
                    if "error" in response:
                        raise ValueError(response["error"]["desc"])
                    if "return" in response:
                        break
            return response["return"]


def running(node: Node, state: State) -> bool:
    """Check monitor ownership before treating a guest as this environment's VM.

    A live PID without a working monitor is an unsafe state, not proof of ownership.
    The saved instance name must match the name reported by QEMU; this prevents a stale
    PID or reused socket path from causing an unrelated process to be stopped.
    Ambiguous resources also prevent cleanup, even when no monitor can be reached.
    """
    for name in MACHINE_RESOURCES:
        resource_path(node, name, state)
    try:
        result = qmp(node, "query-name", state)
    except OSError as error:
        pidfile = resource_path(node, "qemu.pid", state)
        if pidfile.exists():
            # A PID can be reused by the host. Its existence cannot authorize a shutdown.
            pid = int(pidfile.read_text())
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return False
            raise ValueError(
                f"{node.name} has a live PID but no monitor. Check logs before restarting."
            ) from error
        return False
    if result.get("name") != f"labstack-{state.instance_id}-{node.name}":
        raise ValueError(f"The monitor for {node.name} belongs to another VM.")
    return True


def prepare_runtime_directory() -> None:
    """Create private socket storage without trusting an existing /tmp entry.

    The path is predictable so later commands can reconnect to QMP. Reject links
    and directories owned by another account before changing permissions or passing
    their paths to QEMU. This check runs before images or guest resources are created.
    """
    directory = get_settings().runtime_dir
    directory.mkdir(mode=0o700, exist_ok=True)
    if directory.is_symlink() or directory.stat().st_uid != os.getuid():
        raise ValueError(f"Unsafe QEMU runtime directory: {directory}")
    directory.chmod(0o700)


def requirements() -> Path:
    """Check the host tools and locate Homebrew's ARM64 UEFI firmware.

    The supported runner uses macOS on Apple Silicon with HVF. Tool and firmware checks
    happen before creating guest resources, so a missing prerequisite does not leave
    half-created overlays or access credentials.
    """
    if platform.system() != "Darwin" or platform.machine() != "arm64":
        raise ValueError("This environment requires macOS on Apple Silicon.")
    for command in (
        "qemu-system-aarch64",
        "qemu-img",
        "xorriso",
        "ssh",
        "ssh-keygen",
        "curl",
        "tar",
        "rsync",
    ):
        if not shutil.which(command):
            raise ValueError(f"Missing {command}. Run 'task install'.")
    prefix = subprocess.check_output(["brew", "--prefix", "qemu"], text=True).strip()
    firmware = Path(prefix) / "share/qemu"
    for name in ("edk2-aarch64-code.fd", "edk2-arm-vars.fd"):
        if not (firmware / name).is_file():
            raise ValueError(f"Missing firmware: {firmware / name}")
    return firmware


def check_ports(node: Node) -> None:
    """Reject occupied forwarded ports before starting the guest.

    Every mapping binds to localhost. This catches conflicts with another lab or host
    service early; the final QEMU bind still remains the authority if a port is taken
    between this check and VM startup.
    """
    for mapping in node.ports:
        port = mapping.published
        with socket.socket() as connection:
            try:
                connection.bind(("127.0.0.1", port))
            except OSError as error:
                raise ValueError(f"Host port {port} is already in use.") from error
