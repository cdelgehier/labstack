"""Create, resume and remove the resources of one saved VM environment."""

import os
import shutil
import subprocess
import tempfile
import time
import uuid
from pathlib import Path

from labstack.cloud_init import prepare_node
from labstack.images import (
    acquire_image,
    resolve_boxes,
    validate_disk,
    verify_sha256,
)
from labstack.inventory import nodes, resolve_config
from labstack.kubernetes import export_kubeconfig, prepare_client
from labstack.logger import get_logger
from labstack.models.state import State
from labstack.provisioning import provision, validate_scripts
from labstack.qemu import (
    check_ports,
    migrate_resources,
    prepare_runtime_directory,
    qemu_command,
    qmp,
    requirements,
    running,
)
from labstack.settings import get_settings
from labstack.ssh import wait_for_guest, write_ssh_config

logger = get_logger(__name__)


def read_state() -> State:
    """Load the saved inventory, identity and image pins for an existing environment.

    Commands use this state rather than new defaults from machines.yml. Validation
    rejects missing machine paths or inconsistent image pins before those values are
    used to access a disk or control a VM.
    """
    settings = get_settings()
    if not (settings.state_dir / "state.json").exists():
        raise ValueError("No environment exists. Run 'task up' first.")
    return State.model_validate_json((settings.state_dir / "state.json").read_text())


def save_state(state: State, *, path: Path | None = None) -> None:
    """Replace saved state only after the complete new document is on disk.

    A full disk or interrupted write must leave the previous state readable while
    guests are running. Write beside the destination so os.replace stays atomic,
    and keep temporary files private from their creation. Reset uses the same
    writer to preserve its image choices outside the removed state directory.
    """
    destination = path or get_settings().state_dir / "state.json"
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=destination.parent,
            prefix=f".{destination.name}-",
            delete=False,
        ) as stream:
            temporary = Path(stream.name)
            stream.write(state.model_dump_json(indent=2))
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(destination)
        # Persist the directory entry too: reset may remove .state immediately after
        # its recovery record is saved, so the new filename must survive a crash.
        descriptor = os.open(destination.parent, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def validate_images(state: State) -> None:
    """Check unique bases, saved checksums and every guest's requested capacity.

    The same checks protect startup and reset. Checking capacities here prevents
    discovering an undersized disk only after reset has removed the old environment.
    Shared bases are read once for structural checks and once per distinct checksum.
    """
    capacities = {disk: validate_disk(disk) for disk in set(state.base_images.values())}
    checksums = {
        (state.base_images[name], pin.disk_sha256)
        for name, pin in state.resolved_boxes.items()
    }
    for disk, checksum in checksums:
        verify_sha256(disk, checksum)
    for node in nodes(state.config):
        if node.disk_gib * 1024**3 < capacities[state.base_images[node.name]]:
            raise ValueError(f"{node.name}: disk_gib is smaller than the base image.")


def reset() -> None:
    """Check recreation first, then preserve enough information to retry a reset.

    The reset record survives removal of .state, including failures before new keys
    or state can be saved. A retry uses its original topology and pins, and stops any
    partially recreated guests using their own saved identity before trying again.
    """
    settings = get_settings()
    pending = settings.reset_file
    saved = (
        State.model_validate_json(pending.read_text())
        if pending.exists()
        else read_state()
    )
    # Predictable failures must leave the current environment and its disks intact.
    requirements()
    if saved.provisioned:
        validate_scripts(saved.config)
    validate_images(saved)
    prepare_runtime_directory()
    existing = read_state() if (settings.state_dir / "state.json").exists() else None
    for node in nodes(saved.config):
        if existing is None or not running(node, existing):
            check_ports(node)
    if not pending.exists():
        save_state(saved, path=pending)
    if existing is not None:
        destroy(existing, keep_reset_plan=True)
    elif settings.state_dir.exists():
        # No guest can start before state is saved. Only unfinished key setup remains.
        shutil.rmtree(settings.state_dir)
    up(None, None, saved=saved, no_provision=not saved.provisioned)
    pending.unlink()


def up(
    profiles: tuple[str, ...] | None,
    image_path: Path | None,
    saved: State | None = None,
    *,
    no_provision: bool = False,
) -> None:
    """Create missing VM resources or resume the saved environment.

    Fresh environments resolve image references once and save their exact release
    metadata. Existing guests keep their disks, access keys and instance identity.
    A reset passes saved state explicitly to preserve image choices while creating new
    writable resources. No-provision startup still prepares access, networking and disk
    growth, but skips all workload scripts. A failed start keeps state for inspection.
    """
    settings = get_settings()
    if saved is None and settings.reset_file.exists():
        raise ValueError("A reset is incomplete. Run 'task reset' to resume.")
    firmware = requirements()
    existing = read_state() if (settings.state_dir / "state.json").exists() else None
    config = (
        saved.config
        if saved
        else resolve_config(existing.config if existing else None, profiles)
    )
    if not no_provision:
        validate_scripts(config)
    prepare_runtime_directory()
    if existing and image_path:
        raise ValueError(
            "IMAGE_PATH is only used for a new environment. Run 'task destroy' first."
        )
    if existing:
        state = existing
    else:
        # Resolve latest only on creation. Restarts and resets keep the saved release.
        resolved_boxes = (
            saved.resolved_boxes
            if saved
            else ({} if image_path else resolve_boxes(config))
        )
        acquired = {}
        base_images = {}
        for node in nodes(config):
            pin = resolved_boxes.get(node.name)
            source = saved.base_images[node.name] if saved else image_path
            # Clones sharing a base do not need another copy or checksum pass.
            key = (pin, source)
            if key not in acquired:
                acquired[key] = acquire_image(pin, source)
            base_images[node.name] = acquired[key]
        state = State(
            config=config,
            instance_id=uuid.uuid4().hex[:12],
            base_images=base_images,
            resolved_boxes=resolved_boxes,
            resource_layout="namespaced",
        )
        settings.state_dir.mkdir(mode=0o700, exist_ok=True)
        if not (settings.state_dir / "id_ed25519").exists():
            subprocess.run(
                [
                    "ssh-keygen",
                    "-q",
                    "-t",
                    "ed25519",
                    "-N",
                    "",
                    "-C",
                    "labstack",
                    "-f",
                    str(settings.state_dir / "id_ed25519"),
                ],
                check=True,
            )
        # Save the state only after access credentials exist. A retry can finish VM creation.
        save_state(state)
    validate_images(state)
    migrated = migrate_resources(state)
    if migrated != state:
        save_state(migrated)
        state = migrated
    write_ssh_config(state)
    for node in nodes(config):
        if running(node, state):
            continue
        check_ports(node)
        prepare_node(node, state, firmware)
        (settings.runtime_dir / f"qmp-{node.index}.sock").unlink(missing_ok=True)
        if node.name == nodes(config)[0].name:
            for worker in nodes(config)[1:]:
                (settings.runtime_dir / f"bus{worker.index}.sock").unlink(
                    missing_ok=True
                )
        logger.info("Starting machine.", node=node.name, address=node.address)
        subprocess.run(qemu_command(node, state, firmware), check=True)
    for node in nodes(config):
        wait_for_guest(node, state)
    if not no_provision:
        # A failed retry must not leave a saved success flag for automatic checks.
        state = state.model_copy(update={"provisioned": False})
        save_state(state)
        provision(state)
        state = state.model_copy(update={"provisioned": True})
        save_state(state)
        if state.kubernetes_enabled:
            export_kubeconfig(state)
            prepare_client()
    logger.info("Environment ready.", machines=len(nodes(config)))
    if no_provision:
        logger.info("Provisioning skipped for this start.")
    logger.info(
        "Open a guest shell with Task.",
        commands=[f"task connect -- {node.name}" for node in nodes(config)],
    )


def down(state: State) -> None:
    """Stop only the guests owned by this environment, keeping writable resources.

    Workers stop before the first machine so their network hub remains available during
    shutdown. Guests get two minutes after an orderly powerdown request before QEMU
    is stopped. This covers Ubuntu's container stop timeout and disk flushes.
    Ownership is checked throughout; a live VM keeps its disk if shutdown cannot
    be confirmed.
    """
    timeout_seconds = 120
    # Keep the first machine's socket hub alive until all workers have stopped.
    for node in reversed(nodes(state.config)):
        if not running(node, state):
            continue
        logger.info("Stopping machine.", node=node.name)
        qmp(node, "system_powerdown", state)
        # Ubuntu can spend 90 seconds stopping container scopes. Leave time for
        # those jobs and disk writes before falling back to a QEMU stop.
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline and running(node, state):
            time.sleep(1)
        if running(node, state):
            logger.warning(
                "Guest shutdown timed out; stopping QEMU.",
                node=node.name,
                timeout_seconds=timeout_seconds,
            )
            qmp(node, "quit", state)
            time.sleep(1)
        if running(node, state):
            raise ValueError(f"{node.name} is still running. Its disk was kept.")


def destroy(state: State | None = None, *, keep_reset_plan: bool = False) -> None:
    """Remove writable environment data only after every owned guest has stopped.

    Base images and diagnostic logs remain available for reuse and investigation.
    Private keys, shared recipe credentials, overlays and the exported kubeconfig are
    removed together, preventing stale access data from entering the next environment.
    A pending reset can be cancelled even if new state was never created. Current
    state takes priority over its reset record so a partial startup keeps its identity.
    """
    settings = get_settings()
    if state is None:
        state = (
            State.model_validate_json(settings.reset_file.read_text())
            if settings.reset_file.exists()
            and not (settings.state_dir / "state.json").exists()
            else read_state()
        )
    down(state)
    if settings.runtime_dir.exists():
        # Apply the same ownership check before cleanup as before socket creation.
        prepare_runtime_directory()
        shutil.rmtree(settings.runtime_dir)
    if settings.state_dir.exists():
        shutil.rmtree(settings.state_dir)
    (settings.root / "kubeconfig").unlink(missing_ok=True)
    if not keep_reset_plan:
        settings.reset_file.unlink(missing_ok=True)
    logger.info("Environment removed. The image cache and logs were kept.")
