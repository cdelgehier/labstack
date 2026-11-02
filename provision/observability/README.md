# Observability provisioning

This directory will contain the single-VM demo's observability and target-service
recipes.

## Monitoring and alerting

- A demo HTTP server managed by systemd.
- HTTP probes and host metrics.
- Prometheus, Alertmanager and Grafana.

Use a reversible service failure to verify its impact on the dashboard and alert
delivery. Recovery is manual.

## Logs and traces

- Collect Linux service and application logs in Loki.
- Instrument a small backend with OpenTelemetry and export traces to Jaeger.
- Correlate logs and traces for a failed request.

Provisioning scripts are not included yet.
See the [inventory guide](../../docs/recipes.md) to select the lab.
