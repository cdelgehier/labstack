"""Share saved recipes and pinned image metadata across offline tests."""

from pathlib import Path

import pytest

from labstack.models.image import Box


@pytest.fixture
def kubernetes_inventory() -> Path:
    """Use the saved Kubernetes recipe while the active inventory can change."""
    return Path(__file__).resolve().parents[1] / "machines.yml.cks"


@pytest.fixture
def published_box():
    """Describe a published Ubuntu box without reading remote metadata."""
    return Box.model_validate(
        {
            "target": "ubuntu-26.04-arm64",
            "release": "v0.4.0",
            "url": "https://github.com/cdelgehier/image-factory/releases/download/v0.4.0/ubuntu-26.04-arm64.box",
            "box_sha256": "a" * 64,
            "disk_sha256": "b" * 64,
        }
    )
