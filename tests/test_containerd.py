"""Keep the runtime's plugin path aligned with Kubernetes and Cilium."""

import os
import shlex
import shutil
import subprocess
import tomllib

import pytest

from labstack.settings import get_settings


@pytest.mark.parametrize(
    "plugin, cni_options",
    [
        ("io.containerd.grpc.v1.cri", 'bin_dir = "/usr/lib/cni"'),
        (
            "io.containerd.cri.v1.runtime",
            'bin_dir = ""\n      bin_dirs = ["/opt/cni/bin"]',
        ),
    ],
)
def test_runtime_finds_plugins_installed_by_kubernetes_and_cilium(
    tmp_path, plugin, cni_options
):
    """Run the real recipe's config step against Debian and Ubuntu defaults."""
    source = tmp_path / "default.toml"
    source.write_text(
        f'[plugins."{plugin}".cni]\n      {cni_options}\n'
        f'[plugins."{plugin}".containerd.runtimes.runc.options]\n'
        "      SystemdCgroup = false\n"
    )
    commands = tmp_path / "bin"
    commands.mkdir()
    containerd = commands / "containerd"
    containerd.write_text('#!/bin/sh\ncat "$DEFAULT_CONFIG"\n')
    containerd.chmod(0o700)
    # macOS sed uses different in-place flags. Keep the same real regex engine
    # while adapting only that command-line option at the system boundary.
    sed = commands / "sed"
    sed.write_text(
        '#!/bin/sh\nif [ "$1" = -ri ]; then\n'
        '  "$NATIVE_SED" -E "$2" "$3" > "$3.tmp"\n'
        '  mv "$3.tmp" "$3"\n'
        'else\n  exec "$NATIVE_SED" "$@"\nfi\n'
    )
    sed.chmod(0o700)
    output = tmp_path / "config.toml"
    recipe = (get_settings().root / "provision/kubernetes/prepare.sh").read_text()
    start = recipe.index("containerd config default")
    end = recipe.index("systemctl enable --now containerd", start)
    config_step = recipe[start:end].replace(
        "/etc/containerd/config.toml", shlex.quote(str(output))
    )
    native_sed = shutil.which("sed")
    assert native_sed is not None

    subprocess.run(
        ["bash", "-c", config_step],
        env={
            **os.environ,
            "PATH": f"{commands}:{os.environ['PATH']}",
            "DEFAULT_CONFIG": str(source),
            "NATIVE_SED": native_sed,
        },
        check=True,
        capture_output=True,
        text=True,
    )

    runtime = tomllib.loads(output.read_text())["plugins"][plugin]
    cni = runtime["cni"]
    paths = cni.get("bin_dirs") or [cni["bin_dir"]]
    assert paths == ["/opt/cni/bin"]
    assert runtime["containerd"]["runtimes"]["runc"]["options"]["SystemdCgroup"] is True
