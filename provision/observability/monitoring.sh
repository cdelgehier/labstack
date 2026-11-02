#!/usr/bin/env bash
set -euo pipefail
readonly directory=/opt/labstack-observability
# Run host metrics natively so systemd reads do not need container access to D-Bus.
cat >/etc/default/prometheus-node-exporter <<'EOF'
ARGS="--web.listen-address=127.0.0.1:9100 --collector.systemd --collector.systemd.unit-include=demo-httpd[.]service --collector.textfile.directory=/var/lib/labstack-metrics"
EOF
systemctl enable prometheus-node-exporter
systemctl restart prometheus-node-exporter
install -d -m 0755 "$directory" "$directory/grafana" "$directory/grafana/datasources" \
  "$directory/grafana/dashboards" "$directory/dashboard"
install -d -m 0700 -o 65534 -g 65534 "$directory/prometheus-data" "$directory/alertmanager-data" "$directory/ntfy-data"
install -d -m 0700 -o 472 -g 472 "$directory/grafana-data"
# Create a private admin credential once; normal dashboard access is a local viewer.
if [[ ! -f "$directory/grafana-admin-password" ]]; then
  head -c 48 /dev/urandom | base64 >"$directory/grafana-admin-password"
  chown 472:472 "$directory/grafana-admin-password"
  chmod 0400 "$directory/grafana-admin-password"
fi
cat >"$directory/compose.yml" <<'EOF'
name: labstack-observability
x-service: &service
  restart: unless-stopped
  network_mode: host
  user: "65534:65534"
  read_only: true
  cap_drop: [ALL]
  security_opt: [no-new-privileges:true]
  logging:
    driver: json-file
    options: {max-size: 5m, max-file: "2"}
services:
  prometheus:
    <<: *service
    image: prom/prometheus:v3.15.1@sha256:fa3302279710be2f21a8301ec17a8b4f2301fd829bd113839b3b6b02b6f191a2
    command:
      - --config.file=/etc/prometheus/prometheus.yml
      - --storage.tsdb.path=/prometheus
      - --storage.tsdb.retention.time=6h
      - --storage.tsdb.retention.size=256MB
      - --web.external-url=http://127.0.0.1:19090
    mem_limit: 512m
    cpus: 1.0
    tmpfs: ["/tmp:size=64m,mode=1777"]
    volumes:
      - ./prometheus.yml:/etc/prometheus/prometheus.yml:ro
      - ./alerts.yml:/etc/prometheus/alerts.yml:ro
      - ./prometheus-data:/prometheus
  alertmanager:
    <<: *service
    image: prom/alertmanager:v0.34.2@sha256:39d8bb3966c694a124428a3a88fbcffd40bd069129ecd47c059f8a3f17c55f3b
    command:
      - --config.file=/etc/alertmanager/alertmanager.yml
      - --storage.path=/alertmanager
      - --web.external-url=http://127.0.0.1:19093
      - --cluster.listen-address=
    mem_limit: 128m
    cpus: 0.5
    volumes:
      - ./alertmanager.yml:/etc/alertmanager/alertmanager.yml:ro
      - ./alertmanager-data:/alertmanager
  blackbox:
    <<: *service
    image: prom/blackbox-exporter:v0.29.1@sha256:ea1c7aa3ef3f307cd03e85f34abb604478ccb90b567fc690da0cd9776da76b13
    command: [--config.file=/etc/blackbox.yml, --web.listen-address=127.0.0.1:9115]
    mem_limit: 64m
    cpus: 0.5
    volumes: [./blackbox.yml:/etc/blackbox.yml:ro]
  grafana:
    <<: *service
    image: grafana/grafana:13.3.0@sha256:488984b716c0862639f91a698adbe2862677a5a07bad42da5f03f3030883b205
    user: "472:472"
    mem_limit: 384m
    cpus: 1.0
    environment:
      GF_AUTH_ANONYMOUS_ENABLED: "true"
      GF_AUTH_ANONYMOUS_ORG_ROLE: Viewer
      GF_AUTH_DISABLE_LOGIN_FORM: "true"
      GF_USERS_ALLOW_SIGN_UP: "false"
      GF_SECURITY_ADMIN_PASSWORD__FILE: /run/secrets/admin-password
      GF_ANALYTICS_REPORTING_ENABLED: "false"
      GF_ANALYTICS_CHECK_FOR_UPDATES: "false"
      GF_ANALYTICS_CHECK_FOR_PLUGIN_UPDATES: "false"
      GF_SERVER_ROOT_URL: http://127.0.0.1:13000
    tmpfs: [/tmp]
    volumes:
      - ./grafana:/etc/grafana/provisioning:ro
      - ./dashboard:/var/lib/grafana/dashboards:ro
      - ./grafana-data:/var/lib/grafana
      - ./grafana-admin-password:/run/secrets/admin-password:ro
  ntfy:
    <<: *service
    image: binwiederhier/ntfy:v2.37.0@sha256:3176f6f3f0a111c7915a102b94976be92d590b6ff9da198c3d48fa9e50f1b2ef
    command: [serve, --config, /etc/ntfy/server.yml]
    mem_limit: 96m
    cpus: 0.5
    volumes:
      - ./ntfy.yml:/etc/ntfy/server.yml:ro
      - ./ntfy-data:/var/cache/ntfy
EOF
cat >"$directory/prometheus.yml" <<'EOF'
global:
  scrape_interval: 5s
  evaluation_interval: 5s
  external_labels: {environment: demo}
rule_files: [/etc/prometheus/alerts.yml]
alerting:
  alertmanagers:
    - static_configs:
        - targets: [127.0.0.1:9093]
scrape_configs:
  - job_name: prometheus
    static_configs: [{targets: [127.0.0.1:9090]}]
  - job_name: alertmanager
    static_configs: [{targets: [127.0.0.1:9093]}]
  - job_name: node-exporter
    static_configs: [{targets: [127.0.0.1:9100]}]
  - job_name: blackbox
    static_configs: [{targets: [127.0.0.1:9115]}]
  - job_name: demo-httpd
    metrics_path: /probe
    params: {module: [http_demo]}
    static_configs:
      - targets: [http://127.0.0.1:8080/]
        labels: {service: demo-httpd}
    relabel_configs:
      - source_labels: [__address__]
        target_label: __param_target
      - target_label: __address__
        replacement: 127.0.0.1:9115
      - target_label: instance
        replacement: holmes
EOF
cat >"$directory/alerts.yml" <<'EOF'
groups:
  - name: demo
    # Short windows and alert delays are intentional for the live demo.
    rules:
      - alert: DemoHttpUnavailable
        expr: probe_success{job="demo-httpd"} == 0
        for: 10s
        labels: {severity: warning}
        annotations:
          summary: Demo HTTP service is unavailable
          description: >-
            The HTTP probe failed. Check demo-httpd.service and the HTTP response.
            Dashboard and recovery: http://127.0.0.1:13000/d/labstack-httpd
          dashboard_url: http://127.0.0.1:13000/d/labstack-httpd
          runbook_url: http://127.0.0.1:13000/d/labstack-httpd?viewPanel=11
      - alert: DemoMonitoringTargetDown
        expr: up{job=~"node-exporter|blackbox|alertmanager"} == 0
        for: 10s
        labels: {severity: warning, service: monitoring}
        annotations:
          summary: 'Monitoring target {{ $labels.job }} is unreachable'
          description: A failed scrape does not prove that the HTTP application is down.
      - alert: DemoProbeMissing
        expr: absent_over_time(probe_success{job="demo-httpd"}[20s])
        for: 10s
        labels: {severity: warning, service: monitoring}
        annotations:
          summary: HTTP probe telemetry is missing
          description: No recent probe result is available. Check the blackbox exporter and scrape configuration.
      - alert: DemoCpuBusy
        expr: 1 - avg by (instance, job) (rate(node_cpu_seconds_total{job="node-exporter",mode="idle"}[20s])) > 0.85
        for: 10s
        labels: {severity: warning, service: host}
        annotations:
          summary: Host CPU is busy
          description: CPU use stayed above 85% for 10 seconds (demo delay). Check the busiest processes before stopping anything.
      - alert: DemoMemoryLow
        expr: node_memory_MemAvailable_bytes{job="node-exporter"} / node_memory_MemTotal_bytes{job="node-exporter"} < 0.10
        for: 10s
        labels: {severity: warning, service: host}
        annotations:
          summary: Host memory is low
          description: Available memory stayed below 10% for 10 seconds (demo delay). Check process memory and the system journal for OOM kills.
      - alert: DemoDiskSpaceLow
        expr: >-
          (node_filesystem_avail_bytes{job="node-exporter",fstype!~"tmpfs|devtmpfs|squashfs|overlay|nsfs"}
          / node_filesystem_size_bytes) < 0.10
        for: 10s
        labels: {severity: warning, service: storage}
        annotations:
          summary: 'Disk space is low on {{ $labels.mountpoint }}'
          description: Less than 10% of the filesystem is available. Find large files and logs before deleting anything.
      - alert: DemoExpectedMountMissing
        expr: >-
          (labstack_expected_mount{job="node-exporter"} == 1)
          unless on (instance, job, mountpoint)
          (node_filesystem_size_bytes{job="node-exporter",fstype!~"tmpfs|devtmpfs|squashfs|overlay|nsfs"} > 0)
        for: 10s
        labels: {severity: warning, service: storage}
        annotations:
          summary: 'Expected filesystem {{ $labels.mountpoint }} is missing'
          description: The mount is declared as required but is not visible as a usable filesystem. Check findmnt, its mount unit and the journal before mounting it again.
EOF
cat >"$directory/alertmanager.yml" <<'EOF'
route:
  receiver: local-notifications
  group_by: [alertname, service]
  group_wait: 2s
  group_interval: 5s
  repeat_interval: 1h
receivers:
  - name: local-notifications
    webhook_configs:
      - url: http://127.0.0.1:8081/alerts?template=alertmanager
        send_resolved: true
EOF
cat >"$directory/blackbox.yml" <<'EOF'
modules:
  http_demo:
    prober: http
    timeout: 3s
    http:
      preferred_ip_protocol: ip4
      valid_status_codes: [200]
      fail_if_body_not_matches_regexp: [Labstack demo is healthy]
EOF
cat >"$directory/ntfy.yml" <<'EOF'
base-url: http://127.0.0.1:18081
listen-http: :8081
cache-file: /var/cache/ntfy/cache.db
cache-duration: 6h
attachment-cache-dir: ""
EOF
cat >"$directory/grafana/datasources/prometheus.yml" <<'EOF'
apiVersion: 1
datasources:
  - name: Prometheus
    uid: prometheus
    type: prometheus
    access: proxy
    url: http://127.0.0.1:9090
    isDefault: true
    editable: false
EOF
cat >"$directory/grafana/dashboards/demo.yml" <<'EOF'
apiVersion: 1
providers:
  - name: Labstack
    type: file
    options:
      path: /var/lib/grafana/dashboards
EOF
# The same stable UID is used in alert links and in the browser bookmark.
python3 - "$directory/dashboard/httpd.json" <<'PY'
import json
import sys

charts = [
    ("HTTP probe (1 = healthy)", 'probe_success{job="demo-httpd"}', "short", "Probe"),
    ("HTTP service active", 'node_systemd_unit_state{name="demo-httpd.service",state="active"}', "short", "Systemd"),
    ("Probe duration", 'probe_duration_seconds{job="demo-httpd"}', "s", "HTTP"),
    ("HTTP status", 'probe_http_status_code{job="demo-httpd"}', "short", "HTTP status"),
    ("CPU usage", '1 - avg(rate(node_cpu_seconds_total{mode="idle"}[20s]))', "percentunit", "CPU"),
    ("Memory available", "node_memory_MemAvailable_bytes", "bytes", "Available"),
    ("Root filesystem available", 'node_filesystem_avail_bytes{mountpoint="/",fstype!="rootfs"}', "bytes", "Available"),
    ("Active alerts", 'ALERTS{alertstate="firing"}', "short", "{{alertname}}"),
    ("Filesystem available", 'node_filesystem_avail_bytes{fstype!~"tmpfs|devtmpfs|squashfs|overlay|nsfs"} / node_filesystem_size_bytes', "percentunit", "{{mountpoint}}"),
    ("Expected data volume mounted", 'labstack_expected_mount and on(instance,job,mountpoint) (node_filesystem_size_bytes > 0) or (labstack_expected_mount * 0)', "short", "{{mountpoint}}"),
]
panels = []
for index, (title, expression, unit, legend) in enumerate(charts):
    panels.append({
        "id": index + 1, "title": title, "type": "timeseries",
        "gridPos": {"x": index % 2 * 12, "y": index // 2 * 7, "w": 12, "h": 7},
        "datasource": {"type": "prometheus", "uid": "prometheus"},
        "targets": [{"refId": "A", "expr": expression, "legendFormat": legend}],
        "fieldConfig": {"defaults": {"unit": unit}, "overrides": []},
    })
panels.append({
    "id": 11, "title": "HTTP recovery procedure", "type": "text",
    "gridPos": {"x": 0, "y": 35, "w": 24, "h": 6},
    "options": {"mode": "markdown", "content":
        "Inspect the demo VM with `task connect -- holmes`.\n\n"
        "Check: `systemctl status demo-httpd` and `journalctl -u demo-httpd -n 30`.\n\n"
        "Recover: `sudo systemctl start demo-httpd`.\n\n"
        "Confirm that the HTTP probe returns to 1 and the notification is resolved."},
})
with open(sys.argv[1], "w") as output:
    json.dump({"uid": "labstack-httpd", "title": "Labstack HTTP service", "schemaVersion": 39,
               "version": 1, "refresh": "5s", "time": {"from": "now-15m", "to": "now"}, "panels": panels}, output)
PY
cat >/etc/systemd/system/labstack-observability.service <<'EOF'
[Unit]
Description=Labstack observability services
Requires=docker.service
After=docker.service network-online.target
[Service]
Type=oneshot
RemainAfterExit=true
WorkingDirectory=/opt/labstack-observability
ExecStart=/usr/bin/docker compose up -d --remove-orphans
ExecStop=/usr/bin/docker compose down
TimeoutStartSec=600
[Install]
WantedBy=multi-user.target
EOF
cd "$directory"
docker compose config --quiet
docker compose pull --quiet
# Provisioning arrives on stdin; containers must not consume the rest of this script.
docker compose run --rm --no-deps -T --interactive=false --entrypoint /bin/promtool prometheus check config /etc/prometheus/prometheus.yml
docker compose run --rm --no-deps -T --interactive=false --entrypoint /bin/amtool alertmanager check-config /etc/alertmanager/alertmanager.yml
systemctl daemon-reload
systemctl enable labstack-observability
# Apply changed configurations even when this oneshot unit was already active.
docker compose up -d --force-recreate --remove-orphans
systemctl start labstack-observability
for endpoint in http://127.0.0.1:9090/-/ready http://127.0.0.1:9093/-/ready \
  http://127.0.0.1:3000/api/health http://127.0.0.1:8081/v1/health; do
  curl --fail --silent --show-error --retry 30 --retry-all-errors --retry-delay 2 "$endpoint" >/dev/null
done
