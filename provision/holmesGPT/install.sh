#!/usr/bin/env bash
set -euo pipefail
: "${LLM_BASE_URL:?Set LLM_BASE_URL in machines.yml environment}"
: "${LLM_MODEL:?Set LLM_MODEL in machines.yml environment}"
: "${PROVISION_ASSETS_DIR:?Labstack must transfer the recipe assets}"
readonly directory=/opt/labstack-holmes
install -d -m 0755 "$directory" /etc/labstack-holmes
install -d -m 0700 /var/lib/labstack-holmes
install -m 0644 "$PROVISION_ASSETS_DIR/bridge.py" "$PROVISION_ASSETS_DIR/render_config.py" "$directory/"
install -m 0644 "$PROVISION_ASSETS_DIR/toolsets.yaml" /etc/labstack-holmes/toolsets.yaml
# Remove retired procedures too, so Holmes cannot load a stale SOP.
rsync -a --delete "$PROVISION_ASSETS_DIR/sops/" /etc/labstack-holmes/sops/
# Remove the retired demo switch when upgrading an existing VM.
rm -f /usr/local/sbin/holmes-demo-mode /etc/labstack-holmes/mode
if [[ ! -f /etc/labstack-holmes/tool-token ]]; then
  python3 -c 'import secrets; print(secrets.token_hex(32))' >/etc/labstack-holmes/tool-token
  chmod 0600 /etc/labstack-holmes/tool-token
fi
python3 - <<'PY'
from pathlib import Path
directory = Path('/etc/labstack-holmes')
token = (directory / 'tool-token').read_text().strip()
(directory / 'tool-auth').write_text('header = "Authorization: Bearer ' + token + '"\n')
PY
chown root:10001 /etc/labstack-holmes/tool-auth
chmod 0640 /etc/labstack-holmes/tool-auth
cat >"$directory/compose.yml" <<'EOF'
name: labstack-holmes
services:
  holmes:
    image: robustadev/holmes:0.42.0@sha256:7ae20e4012a870fb7a1ce2b4f4e61c9f8adb08283fa3c9a5f7b9438abe351294
    entrypoint: [/bin/sh, -ec]
    command:
      - mkdir -p /tmp/holmes; cp /etc/holmes/config.yaml /tmp/holmes/config.yaml; exec python /app/server.py
    restart: unless-stopped
    network_mode: host
    user: "10001:10001"
    read_only: true
    cap_drop: [ALL]
    security_opt: [no-new-privileges:true]
    mem_limit: 1536m
    cpus: 2.0
    tmpfs: ["/tmp:size=128m,mode=1777"]
    environment:
      HOME: /tmp
      HOLMES_HOST: 127.0.0.1
      HOLMES_PORT: "5050"
      HOLMES_CONFIGPATH_DIR: /tmp/holmes
      MODEL_LIST_FILE_LOCATION: /etc/holmes/model_list.yaml
      ENABLE_TELEMETRY: "false"
      ENABLED_PROMPTS: "-todowrite_instructions"
      MAX_STEPS: "12"
      OVERRIDE_MAX_OUTPUT_TOKEN: "4096"
      LLM_REQUEST_TIMEOUT: "180"
    volumes:
      - /etc/labstack-holmes/config.yaml:/etc/holmes/config.yaml:ro
      - /etc/labstack-holmes/model_list.yaml:/etc/holmes/model_list.yaml:ro
      - /etc/labstack-holmes/toolsets.yaml:/etc/holmes/toolsets.yaml:ro
      - /etc/labstack-holmes/tool-auth:/etc/holmes/tool-auth:ro
      - /etc/labstack-holmes/sops:/etc/holmes/sops:ro
    logging:
      driver: json-file
      options: {max-size: 5m, max-file: "2"}
EOF
cat >/etc/systemd/system/labstack-holmes.service <<'EOF'
[Unit]
Description=HolmesGPT investigation API
Requires=docker.service
After=docker.service labstack-observability.service
[Service]
Type=oneshot
RemainAfterExit=true
EnvironmentFile=/etc/labstack/environment
WorkingDirectory=/opt/labstack-holmes
ExecStartPre=/usr/bin/python3 /opt/labstack-holmes/render_config.py
ExecStart=/usr/bin/docker compose up -d --force-recreate
ExecStop=/usr/bin/docker compose down
TimeoutStartSec=600
[Install]
WantedBy=multi-user.target
EOF
cat >/etc/systemd/system/labstack-holmes-bridge.service <<'EOF'
[Unit]
Description=Holmes demo alert investigations and constrained repairs
Requires=labstack-holmes.service
After=labstack-holmes.service labstack-observability.service
[Service]
ExecStart=/usr/bin/python3 /opt/labstack-holmes/bridge.py
Restart=on-failure
RestartSec=5
UMask=0077
NoNewPrivileges=true
PrivateTmp=true
ProtectHome=true
ProtectSystem=strict
ReadWritePaths=/var/lib/labstack-holmes /srv/demo
CapabilityBoundingSet=CAP_DAC_OVERRIDE CAP_FOWNER
[Install]
WantedBy=multi-user.target
EOF
# Keep raw alerts and Holmes reports on separate local ntfy topics.
python3 - <<'PY'
from pathlib import Path
path = Path('/opt/labstack-observability/alertmanager.yml')
contents = path.read_text()
webhook = 'http://127.0.0.1:18082/alerts'
if webhook not in contents:
    path.write_text(contents + '      - url: ' + webhook + '\n        send_resolved: true\n')
PY
systemctl daemon-reload
systemctl enable labstack-holmes labstack-holmes-bridge
systemctl restart labstack-holmes
curl --fail --silent --show-error --retry 45 --retry-all-errors --retry-delay 2 \
  http://127.0.0.1:5050/readyz >/dev/null
curl --fail --silent --show-error 'http://127.0.0.1:5050/api/info?detail=full' | python3 -c '
import json
import sys
data = json.load(sys.stdin)
enabled = {item["name"] for item in data["toolsets"] if item["status"] == "enabled"}
expected = {"labstack/demo", "skills"}
if enabled != expected or data["toolsets_summary"]["failed"]:
    raise SystemExit("Holmes toolsets are missing, failed or outside the demo boundary")
'
systemctl restart labstack-holmes-bridge
cd /opt/labstack-observability
docker compose run --rm --no-deps -T --interactive=false --entrypoint /bin/amtool \
  alertmanager check-config /etc/alertmanager/alertmanager.yml
docker compose restart alertmanager
curl --fail --silent --show-error --retry 15 --retry-all-errors --retry-delay 1 \
  http://127.0.0.1:18082/healthz >/dev/null
# Model availability must not prevent the presenter from inspecting a failed setup.
if ! curl --fail --silent --show-error --max-time 5 "${LLM_BASE_URL%/}/models" >/dev/null; then
  echo 'The model API is unreachable from the VM. Check the LAN route before injecting a fault.' >&2
fi
