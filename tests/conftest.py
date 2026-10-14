"""Share pinned image metadata across cache and lifecycle tests."""

import pytest

from labstack.models.image import Box


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
