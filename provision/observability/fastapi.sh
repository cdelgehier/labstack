#!/usr/bin/env bash
set -euo pipefail
: "${PROVISION_ASSETS_DIR:?Labstack must transfer the recipe assets}"
readonly directory=/opt/labstack-fastapi

install -d -m 0755 "$directory"
install -m 0644 "$PROVISION_ASSETS_DIR/fastapi_app.py" "$PROVISION_ASSETS_DIR/index.html" \
  "$PROVISION_ASSETS_DIR/requirements-fastapi.txt" "$directory/"
python3 -m venv "$directory/venv"
"$directory/venv/bin/pip" install --disable-pip-version-check -r "$directory/requirements-fastapi.txt"
# Keep data faults reversible. Provisioning must not overwrite an existing message.
mountpoint -q /srv/demo
if [[ ! -e /srv/demo/message.txt ]]; then
  printf 'Labstack demo is healthy\n' >/srv/demo/message.txt
  chmod 0644 /srv/demo/message.txt
fi
cat >/etc/systemd/system/demo-fastapi.service <<'EOF'
[Unit]
Description=Labstack FastAPI demo HTTP service
After=network.target srv-demo.mount
Wants=srv-demo.mount
[Service]
User=www-data
Group=www-data
LogsDirectory=labstack-fastapi
WorkingDirectory=/opt/labstack-fastapi
ExecStart=/opt/labstack-fastapi/venv/bin/uvicorn fastapi_app:create_app --factory --host 0.0.0.0 --port 8080 --no-access-log
Restart=on-failure
Environment=PYTHONUNBUFFERED=1
NoNewPrivileges=true
CapabilityBoundingSet=
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=/var/log/labstack-fastapi
[Install]
WantedBy=multi-user.target
EOF
# Retire the previous two-service recipe before binding its public port.
for legacy_unit in demo-httpd demo-backend apache2; do
  if systemctl cat "$legacy_unit" >/dev/null 2>&1; then
    systemctl disable --now "$legacy_unit"
  fi
done
systemctl daemon-reload
systemctl enable demo-fastapi
systemctl restart demo-fastapi
curl --fail --silent --show-error --retry 5 --retry-connrefused --retry-delay 1 \
  http://127.0.0.1:8080/healthz >/dev/null
