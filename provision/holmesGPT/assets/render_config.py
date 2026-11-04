"""Build Holmes configuration from the VM environment."""

import json
import os
import re
from pathlib import Path
from urllib.parse import urlsplit

# Holmes enables some built-in toolsets by default. Keep the demo's boundary explicit.
DISABLED_TOOLSETS = [
    "crossplane/core",
    "openshift/core",
    "openshift/logs",
    "openshift/live-metrics",
    "openshift/security",
    "helm/core",
    "kubevela/core",
    "kubernetes/core",
    "kubernetes/live-metrics",
    "kubernetes/kube-prometheus-stack",
    "slab",
    "argocd/core",
    "core_investigation",
    "internet",
    "connectivity_check",
    "robusta",
    "grafana/loki",
    "grafana/tempo",
    "newrelic",
    "grafana/dashboards",
    "notion",
    "kafka/admin",
    "datadog/logs",
    "datadog/general",
    "datadog/metrics",
    "datadog/traces",
    "opensearch/query_assist",
    "coralogix",
    "rabbitmq/core",
    "bash",
    "kubectl-run",
    "confluence",
    "MongoDBAtlas",
    "servicenow/tables",
    "victorialogs",
    "database/sql",
    "elasticsearch/data",
    "elasticsearch/cluster",
    "prometheus/metrics",
    "kubernetes/logs",
]


def model_config(environment: dict[str, str]) -> dict:
    """Use any local server with an OpenAI-compatible API and tool calling."""
    base = environment["LLM_BASE_URL"].rstrip("/")
    url = urlsplit(base)
    if (
        url.scheme not in {"http", "https"}
        or not url.hostname
        or url.username
        or url.password
        or url.query
        or url.fragment
        or url.path != "/v1"
    ):
        raise ValueError("LLM_BASE_URL must be an OpenAI-compatible /v1 API URL")
    model = environment["LLM_MODEL"]
    if not re.fullmatch(r"[A-Za-z0-9._/:-]{1,160}", model):
        raise ValueError("Invalid LLM_MODEL")
    extra_body = json.loads(environment.get("LLM_EXTRA_BODY", "{}"))
    if not isinstance(extra_body, dict):
        raise ValueError("LLM_EXTRA_BODY must be a JSON object")
    return {
        "demo": {
            "model": "openai/" + model,
            "api_base": base,
            "api_key": "local",  # Required by LiteLLM; not a credential.
            "custom_args": {"max_context_size": 20480},
            "max_tokens": 4096,
            "temperature": 1.0,
            "extra_body": extra_body,
        }
    }


def write_config(directory: Path) -> None:
    """Register the SOPs and fixed tools using the VM environment."""
    (directory / "model_list.yaml").write_text(
        json.dumps(model_config(dict(os.environ)))
    )
    config = {
        "custom_toolsets": ["/etc/holmes/toolsets.yaml"],
        "custom_skill_paths": ["/etc/holmes/sops"],
        "toolsets": {name: {"enabled": False} for name in DISABLED_TOOLSETS},
    }
    config["toolsets"]["skills"] = {"enabled": True}
    (directory / "config.yaml").write_text(json.dumps(config))


if __name__ == "__main__":
    write_config(Path("/etc/labstack-holmes"))
