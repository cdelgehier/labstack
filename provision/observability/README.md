# HTTP monitoring demo

One VM runs an Apache HTTP service managed by systemd. Blackbox exporter checks
its HTTP status and page content. Node exporter collects host metrics and the
service state. Prometheus evaluates alerts, Alertmanager routes them to a local
ntfy inbox, and Grafana displays the service and host metrics.

A separate ext4 filesystem at `/srv/demo` supports disk-space and missing-mount
demonstrations. It uses a loop image, so no additional virtual disk is needed.
Its required mount is declared in `/var/lib/labstack-metrics/mounts.prom`.

## Start the lab

If another lab exists, run `task destroy` before switching inventories.

```sh
cp machines.yml.holmes machines.yml
task up
```

The forwarded ports listen on the Mac's loopback interface. Grafana opens as a
viewer without a login. Notifications stay inside this lab.

| Page | URL |
| --- | --- |
| Demo HTTP service | <http://127.0.0.1:18080/> |
| Grafana dashboard | <http://127.0.0.1:13000/d/labstack-httpd> |
| Prometheus | <http://127.0.0.1:19090/> |
| Alertmanager | <http://127.0.0.1:19093/> |
| ntfy notifications | <http://127.0.0.1:18081/alerts> |

## Stop a service and receive an alert

Open the dashboard and the ntfy inbox.

Alerts appear in the ntfy inbox. For desktop notifications, click **GRANT NOW**
in ntfy and allow notifications in your browser. An alert that stays active is
sent again after one hour.

Connect to the VM and stop the service:

```sh
task connect -- holmes
sudo systemctl stop demo-httpd
```

The HTTP page becomes unavailable, the probe drops to zero and the systemd
state changes. After 10 seconds of failed probes, `DemoHttpUnavailable` fires.
Allow about 15–25 seconds in total for scraping, rule evaluation and delivery.
ntfy receives a notification with links to the dashboard and recovery procedure.

Inspect and recover the service manually:

```sh
systemctl status demo-httpd
journalctl -u demo-httpd -n 30
sudo systemctl start demo-httpd
```

The probe returns to one, the alert clears and ntfy receives a resolved message.
Scrape failures and missing probe results have separate alerts: missing
telemetry must not be reported as proof of an HTTP application failure.

## Host and storage alerts

| Alert | Condition | Duration |
| --- | --- | --- |
| `DemoCpuBusy` | CPU use above 85%, measured over 20 seconds | 10 seconds |
| `DemoMemoryLow` | Available memory below 10% | 10 seconds |
| `DemoDiskSpaceLow` | Available filesystem space below 10% | 10 seconds |
| `DemoExpectedMountMissing` | Required filesystem missing or unreadable | 10 seconds |

**Demo timing:** observation windows and alert delays are deliberately shortened
so incidents are visible quickly during the presentation. Use longer windows and
delays for normal operation to avoid alerts on brief spikes. Scrapes and rule
evaluation run every 5 seconds; notification grouping adds another 2 seconds.

Memory uses `MemAvailable`, which accounts for reclaimable cache. Disk-space
checks cover real filesystems, including the root and demo data volumes.
A stopped node exporter raises a monitoring alert; it does not prove that a
disk has disappeared. Only explicitly declared mounts are required.

To demonstrate a missing data volume inside the VM:

```sh
sudo systemctl stop srv-demo.mount
findmnt /srv/demo
# Wait for the alert, then restore the volume.
sudo systemctl start srv-demo.mount
```

## Verify the complete path

From the repository on the Mac:

```sh
task test:observability
```

This test checks alert timing and recovery with simulated metrics, including CPU
and memory pressure. It then verifies the healthy baseline, an incorrect HTTP
page, a stopped HTTP service, low disk space and a missing data volume. The disk
test fills only `/srv/demo`; it never fills the VM root filesystem. HTTP service
and storage failures must produce both a firing and a resolved notification.
Cleanup traps restore the page, service and demo volume if a test fails or is
interrupted. Normal provisioning never injects a fault.

## Planned additions

Linux and application logs in Loki, then OpenTelemetry traces in Jaeger, with
Grafana links between the signals. Logs, traces and HolmesGPT are not installed
by these recipes yet.
