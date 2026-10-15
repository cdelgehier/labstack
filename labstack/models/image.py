"""Describe box names and the catalogue and manifest fields required by QEMU."""

from typing import Annotated, Any, Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    HttpUrl,
    field_validator,
    model_validator,
)

from labstack.models.access import GuestUsername

STABLE_VERSION = r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"
SHA256 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class BoxReference(BaseModel):
    """Select an Image Factory target and optionally fix its stable version.

    An unversioned reference is resolved only when an environment is created. The
    resolved archive and disk checksums belong to saved state, not to this reference.
    This keeps the inventory short without letting a restart silently change images.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)
    target: str = Field(pattern=r"^[a-z0-9]+(?:[.-][a-z0-9]+)*$")
    version: str | None = Field(default=None, pattern=f"^{STABLE_VERSION}$")

    @model_validator(mode="before")
    @classmethod
    def parse_name(cls, value: Any) -> Any:
        """Read TARGET[:VERSION] strings and structured references from saved state."""
        if isinstance(value, str):
            target, separator, version = value.removeprefix("cdelgehier/").partition(
                ":"
            )
            return {"target": target, "version": version if separator else None}
        return value


class Box(BaseModel):
    """Record the exact published archive and disk selected for a machine.

    The archive checksum protects the download; the disk checksum protects extraction
    and later cache reuse. These fields are saved together so a reset can keep the same
    image even when the catalogue has changed or is unavailable. The pinned account
    keeps access setup consistent with the image's original public box key.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)
    target: str = Field(pattern=r"^[a-z0-9]+(?:[.-][a-z0-9]+)*$")
    release: str = Field(pattern=f"^v{STABLE_VERSION}$")
    url: HttpUrl
    box_sha256: SHA256
    disk_sha256: SHA256
    # Preserve old Ubuntu pins while recording the account from new build manifests.
    ssh_user: GuestUsername = "ubuntu"

    @field_validator("url")
    @classmethod
    def require_https(cls, value: HttpUrl) -> HttpUrl:
        """Keep image downloads on an encrypted connection."""
        if value.scheme != "https":
            raise ValueError("Box URLs must use HTTPS.")
        return value


class CatalogueProvider(BaseModel):
    """Describe an archive offered for one provider and architecture.

    A release can expose the same archive through several provider entries. Resolution
    selects qemu with arm64; the archive's own format is checked in the build manifest.
    """

    name: str
    architecture: str
    url: HttpUrl
    checksum_type: Literal["sha256"]
    checksum: SHA256


class CatalogueVersion(BaseModel):
    """Group download choices for one published version.

    Preview entries can be present, but automatic selection only considers stable
    versions. Version strings stay intact here so duplicate entries can be rejected.
    """

    version: str = Field(
        pattern=rf"^{STABLE_VERSION}(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?$"
    )
    providers: tuple[CatalogueProvider, ...]


class Catalogue(BaseModel):
    """Validate published versions before choosing a large image download.

    The catalogue identifies the target and supplies archive checksums. It does not
    contain the extracted disk checksum, which must come from the matching manifest.
    """

    name: str
    versions: tuple[CatalogueVersion, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def unique_versions(self) -> Self:
        """Reject ambiguous releases rather than relying on catalogue order."""
        versions = [entry.version for entry in self.versions]
        if len(set(versions)) != len(versions):
            raise ValueError("Catalogue versions must be unique.")
        return self


class ManifestDisk(BaseModel):
    """Require the QCOW2 member that the runner extracts from the archive.

    A fixed member name lets extraction stream only the disk bytes. Other archive
    paths are never unpacked into host directories.
    """

    path: Literal["box_0.img"]
    format: Literal["qcow2"]


class BoxMetadata(BaseModel):
    """Check the archive contract expected by the direct QEMU runner.

    Image Factory exposes a qemu catalogue entry for a libvirt-format archive. The
    runner requires one ARM64 QCOW2 disk; other formats or layouts cannot be imported.
    """

    provider: Literal["libvirt"]
    architecture: Literal["arm64"]
    disks: tuple[ManifestDisk, ...] = Field(min_length=1, max_length=1)


class ManifestArchive(BaseModel):
    """Identify the archive whose checksum must agree with the catalogue.

    Checking both the filename and digest prevents a manifest for another target from
    being accepted just because it was found next to a valid release download.
    """

    filename: str
    sha256: SHA256


class BuildManifest(BaseModel):
    """Validate the Linux guest and disk contract required by this runner.

    The manifest supplies the box's default SSH account. The inventory must agree
    with it so access setup replaces the right account's public box key.
    Incompatible communicators, architectures and disk layouts fail before VM startup.
    Additional factory build fields are outside the runner's metadata contract.
    """

    target: str
    architecture: Literal["arm64"]
    communicator: Literal["ssh"]
    ssh_username: GuestUsername
    box_metadata: BoxMetadata
    box: ManifestArchive
    disk_sha256: SHA256
