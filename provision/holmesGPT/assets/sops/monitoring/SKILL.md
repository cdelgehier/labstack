---
name: demo-monitoring
description: Investigate DemoMonitoringTargetDown, DemoProbeMissing or DemoApiProbeMissing as missing telemetry rather than a proven application outage.
---
1. Call read_demo_metrics, read_demo_status and read_demo_logs once each.
2. Use the alert labels and up == 0 to identify the affected monitoring target.
   Distinguish a failed scrape from a missing probe series and from an actual
   probe_success=0 result. Missing evidence does not mean the service is down.
3. Compare the direct HTTP snapshots and demo-fastapi state with the available
   probe results. Report which signals still work and which are unavailable.
   The status tool does not inspect every monitoring service; do not claim
   an exporter, collector or monitoring process is stopped without evidence.
4. Request an operator check of the affected target's Prometheus scrape error,
   reachability, service state and configuration. Give the specific next check
   supported by the observed labels; do not invent a transport or DNS cause.

No automatic repair is authorized. Never restart monitoring, the application,
Docker or the VM. State the observability gap, any independently observed
application impact and that human intervention is required.
