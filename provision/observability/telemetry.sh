#!/usr/bin/env bash
set -euo pipefail
readonly directory=/opt/labstack-observability
readonly collector_version=0.164.0
install -d -m 0755 "$directory" "$directory/grafana/datasources"

# Use the host journal directly without giving a container access to the host.
if ! id labstack-telemetry >/dev/null 2>&1; then
  useradd --system --no-create-home --shell /usr/sbin/nologin labstack-telemetry
fi
usermod -a -G adm,systemd-journal labstack-telemetry
install -d -m 0700 -o labstack-telemetry -g labstack-telemetry /var/lib/labstack-telemetry
if [[ ! -x /usr/local/bin/otelcol-contrib ]] || \
  ! /usr/local/bin/otelcol-contrib --version | grep -q "version $collector_version"; then
  download_directory=$(mktemp -d)
  trap 'rm -rf "$download_directory"' EXIT
  curl -fsSL --retry 3 --max-time 300 \
    "https://github.com/open-telemetry/opentelemetry-collector-releases/releases/download/v$collector_version/otelcol-contrib_${collector_version}_linux_arm64.tar.gz" \
    -o "$download_directory/collector.tar.gz"
  printf '%s  %s\n' 8bd4a7f443405522a2175cc1a8675f9e99cc047c7181b73461efd08efbba38d3 \
    "$download_directory/collector.tar.gz" | sha256sum --check --status
  tar -xzf "$download_directory/collector.tar.gz" -C "$download_directory" otelcol-contrib
  install -m 0755 "$download_directory/otelcol-contrib" /usr/local/bin/otelcol-contrib
fi
install -d -m 0700 -o 65534 -g 65534 "$directory/loki-data" "$directory/jaeger-data"
cat >"$directory/compose.override.yml" <<'YAML'
services:
  loki:
    image: grafana/loki:3.7.9@sha256:0db216b630ae1f348a1584c67f3cead8b4c1fc839169a39501db664744158e28
    restart: unless-stopped
    network_mode: host
    user: "65534:65534"
    read_only: true
    cap_drop: [ALL]
    security_opt: [no-new-privileges:true]
    command: [-config.file=/etc/loki.yml]
    mem_limit: 384m
    cpus: 1.0
    volumes: [./loki.yml:/etc/loki.yml:ro, ./loki-data:/loki]
    logging:
      driver: json-file
      options: {max-size: 5m, max-file: "2"}
  jaeger:
    # Grafana needs the v1 query API, removed in Jaeger 2.21.
    image: jaegertracing/jaeger:2.20.0@sha256:46a886260e04002d8f45e213fc39063fa11a50446048fdaa64786fc0840cb9f8
    restart: unless-stopped
    network_mode: host
    user: "65534:65534"
    read_only: true
    cap_drop: [ALL]
    security_opt: [no-new-privileges:true]
    command: [--config=/etc/jaeger.yml]
    environment: {OTEL_TRACES_EXPORTER: none}
    mem_limit: 512m
    cpus: 1.0
    volumes: [./jaeger.yml:/etc/jaeger.yml:ro, ./jaeger-data:/badger]
    logging:
      driver: json-file
      options: {max-size: 5m, max-file: "2"}
YAML
cat >"$directory/loki.yml" <<'YAML'
auth_enabled: false
server:
  http_listen_address: 127.0.0.1
  http_listen_port: 3100
  grpc_listen_address: 127.0.0.1
  grpc_listen_port: 9096
common:
  instance_addr: 127.0.0.1
  path_prefix: /loki
  replication_factor: 1
  ring:
    kvstore: {store: inmemory}
  storage:
    filesystem:
      chunks_directory: /loki/chunks
      rules_directory: /loki/rules
schema_config:
  configs:
    - from: 2026-01-01
      store: tsdb
      object_store: filesystem
      schema: v13
      index: {prefix: index_, period: 24h}
compactor:
  working_directory: /loki/compactor
  retention_enabled: true
  delete_request_store: filesystem
limits_config:
  retention_period: 24h
  ingestion_rate_mb: 2
  ingestion_burst_size_mb: 4
  allow_structured_metadata: true
analytics:
  reporting_enabled: false
YAML
cat >"$directory/jaeger.yml" <<'YAML'
service:
  extensions: [jaeger_storage, jaeger_query]
  pipelines:
    traces:
      receivers: [otlp]
      processors: [batch]
      exporters: [jaeger_storage_exporter]
  telemetry:
    metrics:
      readers:
        - pull:
            exporter:
              prometheus: {host: 127.0.0.1, port: 8889}
extensions:
  jaeger_query:
    http: {endpoint: 0.0.0.0:16686}
    storage: {traces: demo}
  jaeger_storage:
    backends:
      demo:
        badger:
          directories: {keys: /badger/keys, values: /badger/values}
          ephemeral: false
          ttl: {spans: 6h}
receivers:
  otlp:
    protocols:
      http: {endpoint: 127.0.0.1:14318}
processors:
  batch: {timeout: 1s}
exporters:
  jaeger_storage_exporter: {trace_storage: demo}
YAML
cat >"$directory/collector.yml" <<'YAML'
extensions:
  file_storage:
    directory: /var/lib/labstack-telemetry
receivers:
  otlp:
    protocols:
      http: {endpoint: 127.0.0.1:4318}
  file_log/fastapi:
    include: [/var/log/labstack-fastapi/requests.jsonl]
    start_at: beginning
    storage: file_storage
    operators:
      - type: json_parser
        timestamp:
          parse_from: attributes.timestamp
          layout_type: gotime
          layout: '2006-01-02T15:04:05.999999Z07:00'
        severity: {parse_from: attributes.level}
  journald:
    directory: /var/log/journal
    units: [demo-fastapi, srv-demo.mount, labstack-observability]
    start_at: beginning
    storage: file_storage
processors:
  memory_limiter:
    check_interval: 1s
    limit_mib: 192
    spike_limit_mib: 32
  batch: {timeout: 1s, send_batch_size: 128}
  resource/fastapi:
    attributes: [{key: service.name, value: demo-fastapi, action: upsert}]
  resource/linux:
    attributes: [{key: service.name, value: linux, action: upsert}]
  transform/journal:
    log_statements:
      - context: log
        statements:
          - set(log.attributes["systemd.unit"], log.body["_SYSTEMD_UNIT"])
          - set(log.body, log.body["MESSAGE"]) where log.body["MESSAGE"] != nil
exporters:
  otlp_http/jaeger:
    endpoint: http://127.0.0.1:14318
    sending_queue: {storage: file_storage, queue_size: 200}
  otlp_http/loki:
    endpoint: http://127.0.0.1:3100/otlp
    sending_queue: {storage: file_storage, queue_size: 200}
service:
  extensions: [file_storage]
  telemetry:
    metrics:
      readers:
        - pull:
            exporter:
              prometheus: {host: 127.0.0.1, port: 8888}
  pipelines:
    traces:
      receivers: [otlp]
      processors: [memory_limiter, batch]
      exporters: [otlp_http/jaeger]
    logs/fastapi:
      receivers: [file_log/fastapi]
      processors: [memory_limiter, resource/fastapi, batch]
      exporters: [otlp_http/loki]
    logs/linux:
      receivers: [journald]
      processors: [memory_limiter, transform/journal, resource/linux, batch]
      exporters: [otlp_http/loki]
YAML
cat >"$directory/grafana/datasources/telemetry.yml" <<'YAML'
apiVersion: 1
datasources:
  - name: Loki
    uid: loki
    type: loki
    access: proxy
    url: http://127.0.0.1:3100
    editable: false
    jsonData:
      derivedFields:
        - name: TraceID
          matcherRegex: '"trace_id":\s*"([a-f0-9]{32})"'
          datasourceUid: jaeger
          url: '$${__value.raw}'
  - name: Jaeger
    uid: jaeger
    type: jaeger
    access: proxy
    url: http://127.0.0.1:16686
    editable: false
    jsonData:
      tracesToLogsV2:
        datasourceUid: loki
        spanStartTimeShift: '-1m'
        spanEndTimeShift: '1m'
        customQuery: true
        query: '{service_name="demo-fastapi"} | json | trace_id="$${__span.traceId}"'
YAML
cat >/etc/systemd/system/labstack-telemetry.service <<'UNIT'
[Unit]
Description=Labstack OpenTelemetry logs and traces collector
Wants=labstack-observability.service
After=labstack-observability.service systemd-journald.service
[Service]
User=labstack-telemetry
Group=labstack-telemetry
ExecStart=/usr/local/bin/otelcol-contrib --config=/opt/labstack-observability/collector.yml
Restart=on-failure
MemoryMax=256M
NoNewPrivileges=true
CapabilityBoundingSet=
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=/var/lib/labstack-telemetry
[Install]
WantedBy=multi-user.target
UNIT
cd "$directory"
/usr/local/bin/otelcol-contrib validate --config=collector.yml
# monitoring.sh starts all services after every configuration has been written.
