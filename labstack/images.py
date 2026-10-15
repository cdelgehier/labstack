"""Resolve image names and verify archives and disks before they enter the cache."""

import hashlib
import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any
from urllib.request import urlopen

from labstack.inventory import nodes
from labstack.logger import get_logger
from labstack.models.config import Config
from labstack.models.image import (
    STABLE_VERSION,
    Box,
    BoxReference,
    BuildManifest,
    Catalogue,
)
from labstack.settings import get_settings

logger = get_logger(__name__)


def read_metadata(url: str) -> Any:
    """Download a small JSON document with bounded size and a timeout.

    Catalogue and manifest URLs should return metadata, not image bytes. A size limit
    catches wrong endpoints before a large download, and failures include the URL so
    a caller can distinguish a missing release from an unavailable catalogue.
    """
    try:
        with urlopen(url, timeout=30) as response:
            # Metadata should stay small; a wrong URL must not download a VM image.
            content = response.read(1024 * 1024 + 1)
        if len(content) > 1024 * 1024:
            raise ValueError("Metadata exceeds 1 MiB.")
        return json.loads(content)
    except (OSError, ValueError) as error:
        raise ValueError(f"Could not read metadata from {url}: {error}") from error


def resolve_box(reference: BoxReference, catalogue_base_url: str) -> Box:
    """Resolve a target reference and verify the matching release manifest.

    Automatic selection uses the highest stable numeric version. A fixed reference
    must exist exactly. The qemu ARM64 archive URL must match the selected factory
    release and target before the manifest is fetched. Both documents must agree on
    the archive checksum; the manifest supplies the standalone disk checksum.
    """
    catalogue_url = f"{catalogue_base_url.rstrip('/')}/{reference.target}.json"
    catalogue = Catalogue.model_validate(read_metadata(catalogue_url))
    if catalogue.name != f"cdelgehier/{reference.target}":
        raise ValueError(f"Catalogue name does not match {reference.target}.")
    candidates = {
        entry.version: entry
        for entry in catalogue.versions
        if re.fullmatch(STABLE_VERSION, entry.version)
    }
    if reference.version:
        version = reference.version
    elif candidates:
        # Numeric comparison keeps 0.10.0 newer than 0.9.0.
        latest = max(tuple(map(int, version.split("."))) for version in candidates)
        version = ".".join(map(str, latest))
    else:
        raise ValueError(f"A stable version of {reference.target} is not published.")
    if version not in candidates:
        raise ValueError(f"{reference.target}:{version} is not published.")
    providers = [
        provider
        for provider in candidates[version].providers
        if provider.name == "qemu" and provider.architecture == "arm64"
    ]
    if len(providers) != 1:
        raise ValueError("The selected version must have one qemu arm64 provider.")
    provider = providers[0]
    release_url = (
        f"https://github.com/cdelgehier/image-factory/releases/download/v{version}"
    )
    filename = f"{reference.target}.box"
    if str(provider.url) != f"{release_url}/{filename}":
        raise ValueError("The box URL does not match the selected release and target.")
    # The catalogue has no disk checksum. Its matching release manifest provides it.
    manifest_url = f"{release_url}/{reference.target}-build-manifest.json"
    manifest = BuildManifest.model_validate(read_metadata(manifest_url))
    if (
        manifest.target != reference.target
        or manifest.box.filename != filename
        or manifest.box.sha256 != provider.checksum
    ):
        raise ValueError("The build manifest does not match the catalogue box.")
    return Box(
        target=reference.target,
        release=f"v{version}",
        url=provider.url,
        box_sha256=provider.checksum,
        disk_sha256=manifest.disk_sha256,
        ssh_user=manifest.ssh_username,
    )


def verify_sha256(path: Path, expected: str) -> None:
    """Compare file bytes with an expected digest before trusting an artifact.

    The same check protects downloads, extracted QCOW2 disks and reused cached files.
    The file is streamed rather than loaded into memory because a disk can be large.
    A mismatch leaves the caller responsible for retaining or removing the failed file.
    """
    with path.open("rb") as stream:
        actual = hashlib.file_digest(stream, "sha256").hexdigest()
    if actual != expected:
        raise ValueError(
            f"SHA-256 mismatch for {path.name}: expected {expected}, got {actual}"
        )


def validate_disk(path: Path) -> int:
    """Require a complete standalone QCOW2 image before sharing it as a base.

    A backing file would make the cache depend on an outside disk that could change or
    disappear. QEMU checks both the declared format and the disk's internal structure;
    a valid overlay is still rejected as a base image. Return its virtual capacity
    so lifecycle checks can reject undersized guests before removing any disks.
    """
    result = subprocess.run(
        ["qemu-img", "info", "--output=json", str(path)],
        check=True,
        capture_output=True,
        text=True,
    )
    info = json.loads(result.stdout)
    if info["format"] != "qcow2" or "backing-filename" in info:
        raise ValueError("The base image must be a standalone QCOW2 disk.")
    subprocess.run(["qemu-img", "check", "-q", str(path)], check=True)
    return info["virtual-size"]


def resolve_boxes(config: Config) -> dict[str, Box]:
    """Resolve each distinct image reference once for a new environment.

    Machines sharing a reference receive the same release and checksums, even if the
    remote catalogue changes during startup. Saved metadata is keyed by machine name
    so later restarts can use those pins without contacting the catalogue.
    """
    pins: dict[BoxReference, Box] = {}
    result = {}
    for node in nodes(config):
        # Resolve shared references together so every clone uses the same release snapshot.
        if node.box not in pins:
            pins[node.box] = resolve_box(node.box, str(config.boxes_catalogue))
        pin = pins[node.box]
        # A different login could leave the image's known public key on its original account.
        if node.ssh_user != pin.ssh_user:
            raise ValueError(
                f"{node.name}: ssh_user must be {pin.ssh_user!r} for {pin.target}."
            )
        result[node.name] = pin
    return result


def acquire_image(pin: Box | None, image_path: Path | None = None) -> Path:
    """Return a checked, read-only base disk from a release or a local build.

    Published boxes are verified before extracting only box_0.img. Local build disks
    are copied so later writes to the original cannot change running guests. During a
    reset, an already cached disk is reused and checked against its saved release pin.
    Cache filenames use disk digests, allowing several machines to share one base.
    """
    settings = get_settings()
    settings.cache_dir.mkdir(exist_ok=True)
    if image_path:
        source = image_path.expanduser().resolve(strict=True)
        validate_disk(source)
        with source.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        # Reset supplies a cached path as well as its saved pin; recomputing a digest alone
        # would accept a changed base instead of checking that it is still the selected image.
        if pin and digest != pin.disk_sha256:
            raise ValueError(
                f"SHA-256 mismatch for {source.name}: expected {pin.disk_sha256}, got {digest}"
            )
        # A reset already uses a cached disk. Keep it instead of making a second copy.
        disk = (
            source
            if source.parent == settings.cache_dir.resolve()
            else settings.cache_dir / f"base-{digest}.qcow2"
        )
        if not disk.exists():
            shutil.copyfile(source, disk)
        verify_sha256(disk, digest)
    else:
        if pin is None:
            raise ValueError("A published box or IMAGE_PATH is required.")
        # The digest shares identical disks and separates different images with the same tag.
        disk = settings.cache_dir / f"base-{pin.disk_sha256}.qcow2"
        if not disk.exists():
            archive = settings.cache_dir / f"box-{pin.box_sha256}.box"
            temporary = archive.with_suffix(".box.part")
            if not archive.exists():
                logger.info("Downloading box.", target=pin.target, release=pin.release)
                subprocess.run(
                    [
                        "curl",
                        "--fail",
                        "--location",
                        "--retry",
                        "3",
                        "--proto",
                        "=https",
                        "--output",
                        str(temporary),
                        str(pin.url),
                    ],
                    check=True,
                )
                verify_sha256(temporary, pin.box_sha256)
                temporary.replace(archive)
            verify_sha256(archive, pin.box_sha256)
            extracted = disk.with_suffix(".part")
            # Read only the disk member. No archive paths are written to the host.
            with extracted.open("wb") as stream:
                subprocess.run(
                    ["tar", "-xOf", str(archive), "box_0.img"],
                    stdout=stream,
                    check=True,
                )
            verify_sha256(extracted, pin.disk_sha256)
            validate_disk(extracted)
            extracted.replace(disk)
            archive.unlink()
        verify_sha256(disk, pin.disk_sha256)
    validate_disk(disk)
    # Every overlay reads this parent. A write to the base would change all its guests.
    disk.chmod(0o444)
    return disk.resolve()
