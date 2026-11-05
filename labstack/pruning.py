"""Prune project-local base images after checking every saved disk reference."""

import json
import re
import subprocess
from pathlib import Path

from labstack.models.state import State
from labstack.settings import get_settings

BASE_IMAGE_NAME = re.compile(r"base-[a-f0-9]{64}\.qcow2")


def backing_images(disk: Path) -> set[Path]:
    """Read a stopped or running overlay's complete QCOW2 backing chain.

    Force-share allows metadata reads while QEMU holds the disk open. A missing,
    unreadable or invalid chain stops pruning before any candidate is removed.
    """
    result = subprocess.run(
        [
            "qemu-img",
            "info",
            "--force-share",
            "--backing-chain",
            "--output=json",
            str(disk.resolve()),
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    layers = json.loads(result.stdout)
    if not isinstance(layers, list) or not layers:
        raise ValueError(f"Could not read the QCOW2 backing chain: {disk}")
    images = set()
    for layer in layers:
        if (
            not isinstance(layer, dict)
            or layer.get("format") != "qcow2"
            or not isinstance(layer.get("filename"), str)
        ):
            raise ValueError(f"Invalid QCOW2 backing chain: {disk}")
        images.add(Path(layer["filename"]).resolve())
    return images


def protected_images() -> set[Path]:
    """Keep state pins, pending reset pins and bases used by actual overlays."""
    settings = get_settings()
    images = set()
    for record in (settings.state_dir / "state.json", settings.reset_file):
        try:
            state = State.model_validate_json(record.read_text())
        except FileNotFoundError as error:
            if record.is_symlink():
                raise ValueError(
                    f"Unreadable saved image references: {record}"
                ) from error
            continue
        images.update(
            (settings.root / path).resolve() for path in state.base_images.values()
        )
    # Both layouts can survive an interrupted migration, even without state.json.
    # Directory iteration must raise on access errors; glob can hide unreadable VMs.
    disks = set()
    for parent in (settings.state_dir, settings.state_dir / "machines"):
        try:
            directories = list(parent.iterdir())
        except FileNotFoundError:
            continue
        for directory in directories:
            if not directory.is_dir():
                continue
            disk = directory / "disk.qcow2"
            try:
                disk.lstat()
            except FileNotFoundError:
                continue
            disks.add(disk)
    for disk in sorted(disks):
        images.update(backing_images(disk))
    return images


def prune_images(*, dry_run: bool = False) -> list[Path]:
    """Remove only unused regular base disks from this project's .cache directory.

    Other projects may use a custom cache, so it cannot be pruned from one lab's
    state. Keep symlinks, kubectl, partial downloads and files with other names.
    The CLI holds the same lifecycle lock as startup and reset during this work.
    """
    settings = get_settings()
    cache = settings.cache_dir
    if cache.resolve() != settings.root / ".cache":
        raise ValueError("Image pruning requires the project's .cache directory.")
    if not cache.exists():
        return []
    protected = protected_images()
    unused = [
        image
        for image in sorted(cache.iterdir())
        if BASE_IMAGE_NAME.fullmatch(image.name)
        and not image.is_symlink()
        and image.is_file()
        and image.resolve() not in protected
    ]
    if not dry_run:
        for image in unused:
            image.unlink()
    return unused
