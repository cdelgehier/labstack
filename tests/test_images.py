import copy
import io
import json

import pytest
from pydantic import ValidationError

from labstack import images
from labstack.models.image import BoxReference


class MetadataHTTPStub:
    """Serve fixed JSON responses at the HTTP boundary without network access."""

    def __init__(self, responses):
        self.responses = responses
        self.requests = []

    def __call__(self, url, timeout):
        self.requests.append(url)
        payload = self.responses[url]
        if isinstance(payload, Exception):
            raise payload
        data = (
            payload.encode()
            if isinstance(payload, str)
            else json.dumps(payload).encode()
        )
        return io.BytesIO(data)


@pytest.fixture
def published_metadata(monkeypatch):
    """Use the same catalogue and manifest fields as an Image Factory release."""
    box_url = "https://github.com/cdelgehier/image-factory/releases/download/v0.4.0/ubuntu-26.04-arm64.box"
    responses = {
        "https://cdelgehier.github.io/image-factory/ubuntu-26.04-arm64.json": {
            "name": "cdelgehier/ubuntu-26.04-arm64",
            "versions": [
                {
                    "version": "0.4.0",
                    "providers": [
                        {
                            "name": "qemu",
                            "architecture": "arm64",
                            "url": box_url,
                            "checksum_type": "sha256",
                            "checksum": "a" * 64,
                        }
                    ],
                }
            ],
        },
        box_url.removesuffix(".box") + "-build-manifest.json": {
            "target": "ubuntu-26.04-arm64",
            "architecture": "arm64",
            "communicator": "ssh",
            "ssh_username": "ubuntu",
            "box": {"filename": "ubuntu-26.04-arm64.box", "sha256": "a" * 64},
            "disk_sha256": "b" * 64,
            "box_metadata": {
                "provider": "libvirt",
                "architecture": "arm64",
                "disks": [{"path": "box_0.img", "format": "qcow2"}],
            },
        },
    }
    stub = MetadataHTTPStub(responses)
    monkeypatch.setattr(images, "urlopen", stub)
    return stub


def test_box_name_resolves_the_archive_and_disk_checksums(published_metadata):
    reference = BoxReference.model_validate("ubuntu-26.04-arm64")

    pin = images.resolve_box(reference, "https://cdelgehier.github.io/image-factory/")

    assert pin.target == "ubuntu-26.04-arm64"
    assert pin.release == "v0.4.0"
    assert pin.box_sha256 == "a" * 64
    assert pin.disk_sha256 == "b" * 64
    assert str(pin.url).endswith("/v0.4.0/ubuntu-26.04-arm64.box")
    assert published_metadata.requests == list(published_metadata.responses)


def test_latest_version_is_selected_numerically(published_metadata):
    catalogue_url, manifest_url = published_metadata.responses
    catalogue = published_metadata.responses[catalogue_url]
    original = catalogue["versions"][0]
    catalogue["versions"] = []
    for version in ("0.9.0", "0.10.0", "0.4.0"):
        entry = copy.deepcopy(original)
        entry["version"] = version
        entry["providers"][0]["url"] = entry["providers"][0]["url"].replace(
            "v0.4.0", "v" + version
        )
        catalogue["versions"].append(entry)
    published_metadata.responses[manifest_url.replace("v0.4.0", "v0.10.0")] = (
        published_metadata.responses[manifest_url]
    )

    pin = images.resolve_box(
        BoxReference.model_validate("ubuntu-26.04-arm64"),
        "https://cdelgehier.github.io/image-factory/",
    )

    assert pin.release == "v0.10.0"


def test_an_explicit_version_does_not_select_a_newer_release(published_metadata):
    catalogue_url, manifest_url = published_metadata.responses
    catalogue = published_metadata.responses[catalogue_url]
    newer = copy.deepcopy(catalogue["versions"][0])
    newer["version"] = "0.5.0"
    newer["providers"][0]["url"] = newer["providers"][0]["url"].replace(
        "v0.4.0", "v0.5.0"
    )
    catalogue["versions"].insert(0, newer)

    pin = images.resolve_box(
        BoxReference.model_validate("ubuntu-26.04-arm64:0.4.0"),
        "https://cdelgehier.github.io/image-factory/",
    )

    assert pin.release == "v0.4.0"
    assert published_metadata.requests[-1] == manifest_url


def test_preview_versions_are_skipped(published_metadata):
    catalogue_url = next(iter(published_metadata.responses))
    catalogue = published_metadata.responses[catalogue_url]
    preview = copy.deepcopy(catalogue["versions"][0])
    preview["version"] = "0.5.0-rc1"
    catalogue["versions"].insert(0, preview)

    pin = images.resolve_box(
        BoxReference.model_validate("ubuntu-26.04-arm64"),
        "https://cdelgehier.github.io/image-factory/",
    )

    assert pin.release == "v0.4.0"


def test_preview_only_catalogue_has_no_stable_release(published_metadata):
    catalogue_url = next(iter(published_metadata.responses))
    published_metadata.responses[catalogue_url]["versions"][0]["version"] = "0.5.0-rc1"

    with pytest.raises(ValueError, match="stable version.*not published"):
        images.resolve_box(
            BoxReference.model_validate("ubuntu-26.04-arm64"),
            "https://cdelgehier.github.io/image-factory/",
        )
    assert published_metadata.requests == [catalogue_url]


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/cdelgehier/image-factory/releases/download/v0.5.0/ubuntu-26.04-arm64.box",
        "https://github.com/cdelgehier/image-factory/releases/download/v0.4.0/debian-13-arm64.box",
        "https://example.com/ubuntu-26.04-arm64.box",
        "http://github.com/cdelgehier/image-factory/releases/download/v0.4.0/ubuntu-26.04-arm64.box",
    ],
)
def test_valid_urls_must_point_to_the_selected_factory_release(published_metadata, url):
    catalogue_url = next(iter(published_metadata.responses))
    catalogue = published_metadata.responses[catalogue_url]
    catalogue["versions"][0]["providers"][0]["url"] = url

    with pytest.raises(ValueError, match="box URL"):
        images.resolve_box(
            BoxReference.model_validate("ubuntu-26.04-arm64"),
            "https://cdelgehier.github.io/image-factory/",
        )
    assert published_metadata.requests == [catalogue_url]


def test_missing_versions_have_a_clear_error(published_metadata):
    with pytest.raises(ValueError, match="not published"):
        images.resolve_box(
            BoxReference.model_validate("ubuntu-26.04-arm64:0.3.0"),
            "https://cdelgehier.github.io/image-factory/",
        )
    assert len(published_metadata.requests) == 1


@pytest.mark.parametrize(
    "change",
    [
        "name",
        "duplicate_version",
        "checksum",
        "checksum_type",
        "architecture",
        "provider",
        "url",
        "empty",
        "duplicate_provider",
    ],
)
def test_invalid_catalogue_entries_stop_before_reading_a_manifest(
    published_metadata, change
):
    catalogue_url = next(iter(published_metadata.responses))
    catalogue = published_metadata.responses[catalogue_url]
    if change == "name":
        catalogue["name"] = "cdelgehier/debian-13-arm64"
    elif change == "duplicate_version":
        catalogue["versions"].append(copy.deepcopy(catalogue["versions"][0]))
    elif change == "empty":
        catalogue["versions"] = []
    elif change == "duplicate_provider":
        catalogue["versions"][0]["providers"].append(
            copy.deepcopy(catalogue["versions"][0]["providers"][0])
        )
    else:
        field = {"provider": "name"}.get(change, change)
        catalogue["versions"][0]["providers"][0][field] = "invalid"

    with pytest.raises(ValueError):
        images.resolve_box(
            BoxReference.model_validate("ubuntu-26.04-arm64"),
            "https://cdelgehier.github.io/image-factory/",
        )
    assert published_metadata.requests == [catalogue_url]


@pytest.mark.parametrize(
    "change",
    [
        "target",
        "checksum",
        "disk_checksum",
        "account",
        "architecture",
        "provider",
        "format",
        "path",
        "filename",
        "communicator",
    ],
)
def test_manifest_must_match_the_selected_arm64_box(published_metadata, change):
    _, manifest_url = published_metadata.responses
    manifest = published_metadata.responses[manifest_url]
    if change == "checksum":
        manifest["box"]["sha256"] = "c" * 64
    elif change == "filename":
        manifest["box"]["filename"] = "debian-13-arm64.box"
    elif change == "disk_checksum":
        manifest["disk_sha256"] = "invalid"
    elif change in {"provider", "format", "path"}:
        if change == "provider":
            manifest["box_metadata"]["provider"] = "virtualbox"
        else:
            manifest["box_metadata"]["disks"][0][change] = "invalid"
    else:
        field = {"account": "ssh_username"}.get(change, change)
        manifest[field] = "invalid"

    with pytest.raises(ValueError):
        images.resolve_box(
            BoxReference.model_validate("ubuntu-26.04-arm64"),
            "https://cdelgehier.github.io/image-factory/",
        )


@pytest.mark.parametrize("payload", ["not JSON", OSError("connection failed")])
def test_metadata_download_errors_are_reported_with_the_url(
    published_metadata, payload
):
    url = next(iter(published_metadata.responses))
    published_metadata.responses[url] = payload

    with pytest.raises(ValueError, match="ubuntu-26.04-arm64.json"):
        images.resolve_box(
            BoxReference.model_validate("ubuntu-26.04-arm64"),
            "https://cdelgehier.github.io/image-factory/",
        )


@pytest.mark.parametrize(
    "reference",
    [
        "../ubuntu",
        "other/ubuntu",
        "ubuntu:latest",
        "ubuntu:",
        "ubuntu:0.4",
        "ubuntu:0.4.0-rc1",
        "ubuntu:00.4.0",
    ],
)
def test_box_names_cannot_select_paths_or_unstable_versions(reference):
    with pytest.raises(ValidationError):
        BoxReference.model_validate(reference)


def test_metadata_larger_than_one_mib_is_rejected(published_metadata):
    url = next(iter(published_metadata.responses))
    published_metadata.responses[url] = " " * (1024 * 1024 + 1)

    with pytest.raises(ValueError, match="exceeds 1 MiB"):
        images.resolve_box(
            BoxReference.model_validate("ubuntu-26.04-arm64"),
            "https://cdelgehier.github.io/image-factory/",
        )


def test_namespaced_reference_has_the_same_target():
    assert BoxReference.model_validate(
        "cdelgehier/ubuntu-26.04-arm64:0.4.0"
    ) == BoxReference.model_validate("ubuntu-26.04-arm64:0.4.0")
