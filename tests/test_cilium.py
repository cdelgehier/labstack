"""Check that an invalid download cannot reach the guest installation step."""

import os
import subprocess
import sys

from labstack.settings import get_settings


def test_cilium_rejects_a_changed_download_before_unpacking_or_installing(tmp_path):
    curl_stub = tmp_path / "curl"
    curl_stub.write_text(
        f"#!{sys.executable}\n"
        "import sys\n"
        "from pathlib import Path\n"
        "output = sys.argv[sys.argv.index('--output') + 1]\n"
        "Path(output).write_bytes(b'changed upstream archive')\n"
    )
    curl_stub.chmod(0o700)
    for command in ("tar", "kubectl"):
        executable = tmp_path / command
        executable.write_text('#!/bin/sh\ntouch "$INSTALL_MARKER"\nexit 1\n')
        executable.chmod(0o700)
    marker = tmp_path / "installation-started"
    temporary_files = tmp_path / "temporary"
    temporary_files.mkdir()

    result = subprocess.run(
        ["bash", str(get_settings().root / "provision/kubernetes/cilium.sh")],
        env={
            **os.environ,
            "PATH": f"{tmp_path}:{os.environ['PATH']}",
            "NODE_IP": "192.168.76.10",
            "INSTALL_MARKER": str(marker),
            "TMPDIR": str(temporary_files),
        },
        text=True,
        capture_output=True,
    )

    assert result.returncode != 0
    assert "FAILED" in result.stdout + result.stderr
    assert not marker.exists()
    assert list(temporary_files.iterdir()) == []
