"""Keep optional Mac-side Kubernetes tools aligned with the guest recipe."""

import json
import subprocess
from pathlib import Path

from labstack.images import verify_sha256
from labstack.inventory import primary_node
from labstack.models.state import State
from labstack.settings import KUBECTL_SHA256, KUBERNETES_VERSION, get_settings
from labstack.ssh import remote


def prepare_client() -> Path:
    """Cache the exact Mac kubectl version used by the guest Kubernetes recipe.

    The download and any existing cached binary are checked against the version's
    pinned checksum. This client is optional and does not replace the user's kubectl
    or edit their global Kubernetes configuration.
    """
    settings = get_settings()
    settings.cache_dir.mkdir(exist_ok=True)
    client = settings.cache_dir / f"kubectl-{KUBERNETES_VERSION}"
    if not client.exists():
        temporary = client.with_suffix(".part")
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
                f"https://dl.k8s.io/release/v{KUBERNETES_VERSION}/bin/darwin/arm64/kubectl",
            ],
            check=True,
        )
        verify_sha256(temporary, KUBECTL_SHA256)
        temporary.chmod(0o700)
        temporary.replace(client)
    verify_sha256(client, KUBECTL_SHA256)
    return client


def export_kubeconfig(state: State) -> None:
    """Copy guest admin access and use the control plane's published host port.

    The guest address is replaced with the localhost endpoint reachable from the Mac.
    The control plane certificate must already cover that address; TLS verification is
    kept enabled. The exported credentials remain private in the project kubeconfig.
    """
    settings = get_settings()
    cp = primary_node(state.config)
    if cp.api_port is None:
        raise ValueError("Publish guest port 6443 before exporting a kubeconfig.")
    text = remote(
        cp,
        "sudo kubectl --kubeconfig=/etc/kubernetes/admin.conf config view --raw -o json",
    )
    config = json.loads(text)
    for entry in config["clusters"]:
        entry["cluster"]["server"] = f"https://127.0.0.1:{cp.api_port}"
    path = settings.root / "kubeconfig"
    path.write_text(json.dumps(config, indent=2) + "\n")
    path.chmod(0o600)
