# HTTP monitoring demo

One VM runs a FastAPI/Uvicorn HTTP service managed by systemd (`demo-fastapi`). Blackbox exporter checks
its HTTP status and page content. Node exporter collects host metrics and the
service state. Prometheus evaluates alerts, Alertmanager routes them to a local
ntfy inbox, and Grafana displays the service and host metrics.

FastAPI also serves `/api/check`. This request reads a demo file. The FastAPI
instrumentation creates the HTTP server span automatically; a manual child span
records the file read. JSON request logs carry the same trace ID. A native OpenTelemetry
collector sends application and Linux service logs to Loki, and traces
to Jaeger. Grafana links request logs and traces through the same `trace_id`.

A separate ext4 filesystem at `/srv/demo` supports disk-space and missing-mount
demonstrations. It uses a loop image, so no additional virtual disk is needed.
Its required mount is declared in `/var/lib/labstack-metrics/mounts.prom`.

## Start the lab

If another lab exists, run `task destroy` before switching inventories.

```sh
cp machines.yml.holmes machines.yml
task up
```

The forwarded ports listen on the Mac's loopback interface. Grafana opens without
a login, with the Editor role so the presenter can use Explore. Notifications
stay inside this lab.

Run `task urls` inside the VM to print these links for the Mac browser.

| Page | URL |
| --- | --- |
| Demo HTTP service | <http://127.0.0.1:18080/> |
| Grafana dashboard | <http://127.0.0.1:13000/d/labstack-fastapi> |
| Prometheus | <http://127.0.0.1:19090/> |
| Alertmanager | <http://127.0.0.1:19093/> |
| ntfy alerts | <http://127.0.0.1:18081/alerts> |
| ntfy Holmes reports | <http://127.0.0.1:18081/holmes> |
| Traced API request | <http://127.0.0.1:18080/api/check> |
| Interactive API documentation | <http://127.0.0.1:18080/docs> |
| Jaeger trace explorer | <http://127.0.0.1:16686/> |

## Inject and recover demo faults

The VM has its own Taskfile. Connect with `task connect -- holmes`, then run:

```sh
task                         # List the demo tasks
task fault:service           # Stop FastAPI
task restore:service         # Start it again
task fault:permissions       # Let Holmes investigate and restore file permissions
task restore                # Recover all demo targets and verify the API
task status
```

| Fault task | Effect | Recovery task |
| --- | --- | --- |
| `fault:service` | Stop `demo-fastapi`; HTTP probe fails | `restore:service` |
| `fault:permissions` | Set message permissions to `000`; API returns 503 | `restore:permissions` |
| `fault:file` | Move the message aside; API returns 503 | `restore:file` |
| `fault:disk` | Leave 5% available on the dedicated demo filesystem | `restore:disk` |
| `fault:mount` | Unmount the expected demo filesystem | `restore:mount` |
| `fault:cpu` | Saturate the VM CPUs for up to two minutes | `restore:cpu` |
| `fault:memory` | Push available RAM below the alert threshold for up to two minutes | `restore:memory` |

Fault tasks leave Holmes running with its SOPs available. It can recover the service, permissions and the identified CPU stress job. Memory,
storage and monitoring faults require human recovery.
Wait for the report and resolved alert before starting another scenario. Repairs
also have a one-minute global cooldown.

The missing-file scenario preserves the original message and refuses to overwrite
an existing saved copy. Disk filling requires the mounted demo loop image, so it
cannot fill the root filesystem. CPU and memory jobs have a systemd runtime limit;
the memory job also has a cgroup memory limit and no swap allowance. If other
workloads consume more RAM during the test, this job is the preferred OOM victim.
It fills its pages once, then sleeps while holding them. Expect a brief CPU spike
during allocation; steady CPU saturation belongs to the separate CPU scenario.
Use the matching recovery task to stop pressure early. `restore` recovers the
mount, original message, permissions, free space and service, and stops both jobs.

From the repository on the Mac, run the checks through SSH:

```sh
ssh -F .state/ssh_config holmes 'sudo bash -se' < tests/demo-tasks.sh
```

These checks temporarily pause the Holmes bridge so automatic repairs do not
hide the injected faults.

## Stop a service and receive an alert

Open the dashboard and the ntfy inbox.

Alerts appear in the ntfy inbox. For desktop notifications, click **GRANT NOW**
in ntfy and allow notifications in your browser. An alert that stays active is
sent again after one hour.

Connect to the VM and stop the service:

```sh
task connect -- holmes
sudo systemctl stop demo-fastapi
```

The HTTP page becomes unavailable, the probe drops to zero and the systemd
state changes. After 10 seconds of failed probes, `DemoHttpUnavailable` fires.
Allow about 15–25 seconds in total for scraping, rule evaluation and delivery.
ntfy receives a notification with links to the dashboard and recovery procedure.

Inspect and recover the service manually:

```sh
systemctl status demo-fastapi
journalctl -u demo-fastapi -n 30
sudo systemctl start demo-fastapi
```

The probe returns to one, the alert clears and ntfy receives a resolved message.
Scrape failures and missing probe results have separate alerts: missing
telemetry must not be reported as proof of an HTTP application failure.

The HTTP service and API dependency have separate probes. The API failure alert
requires a healthy home-page probe, so stopping FastAPI does not create a second
API incident. Alertmanager also inhibits the dependent API notification
while the service-unavailable alert is active.

Chrony keeps the VM clock aligned for log and trace correlation. This disposable
demo permits clock steps after a long VM pause; it does not use a gradual
correction for a large resume offset. Check `chronyc tracking` after suspending
the Mac or VM, and let synchronization finish before injecting a fault.

If Holmes is installed, it may recover the service or file permissions while
you inspect the fault. The automated fault checks pause its bridge while testing
manual recovery, then restore its previous state.

## Follow a failed request from its log to its trace

Open the API URL. Its JSON response includes a `trace_id`; the same ID appears
in the API log and its Jaeger trace. Probes also create requests every five
seconds so the dashboard has fresh examples.

In Grafana's **API logs**, open a row's **Log menu**, then **Show log details**.
Under **Links**, follow **Jaeger** next to **TraceID**. The trace shows the HTTP
request and its child operation, **read demo message**.
The trace's logs link returns to Loki with a filter for that request.
**Explore API traces** opens the trace search directly.

To cause a real file permission failure, connect to the VM and run:

```sh
sudo chmod 000 /srv/demo/message.txt
curl -i http://127.0.0.1:8080/api/check
```

The API returns HTTP 503. FastAPI's home page still responds normally. In Grafana,
find the API error log and follow its TraceID: the child span records a
`PermissionError` while reading the demo file. `DemoApiUnavailable` fires
after ten seconds of failed probes and reaches the ntfy inbox.

Restore access and repeat the request:

```sh
sudo chmod 0644 /srv/demo/message.txt
curl -i http://127.0.0.1:8080/api/check
```

The API returns HTTP 200, new traces succeed and the alert resolves. Holmes's
guarded tool can also restore these permissions.

Loki retains logs for 24 hours; Jaeger retains traces for six hours. Both use
local VM storage that survives a service restart and is removed by `task destroy`.
The collector keeps file offsets, journal cursors and export queues on disk to
handle restarts and temporary export failures. Trace IDs stay in log content
and metadata rather than indexed Loki labels.

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
ssh -F .state/ssh_config holmes 'sudo tee /opt/labstack-observability/rule-tests.yml >/dev/null' < tests/observability-rules.yml
ssh -F .state/ssh_config holmes 'cd /opt/labstack-observability && sudo docker compose run --rm --no-deps -T --interactive=false -v /opt/labstack-observability/rule-tests.yml:/etc/prometheus/tests.yml:ro --entrypoint /bin/promtool prometheus test rules /etc/prometheus/tests.yml'
ssh -F .state/ssh_config holmes 'sudo bash -se' < tests/observability.sh
ssh -F .state/ssh_config holmes 'sudo bash -se' < tests/observability-storage.sh
ssh -F .state/ssh_config holmes 'sudo bash -se' < tests/telemetry.sh
```

This test checks alert timing and recovery with simulated metrics, including CPU
and memory pressure. It then verifies the healthy baseline, an incorrect HTTP
page, a stopped HTTP service, low disk space and a missing data volume. The disk
test fills only `/srv/demo`; it never fills the VM root filesystem. HTTP service
and storage failures must produce both a firing and a resolved notification.
Cleanup traps restore the page, service and demo volume if a test fails or is
interrupted. Normal provisioning never injects a fault.

The telemetry test checks that a successful request and a permission failure
each reach Loki and Jaeger with matching trace IDs. A missing-file scenario
checks that `FileNotFoundError` is distinguished from `PermissionError`. It also
checks FastAPI and Linux log collection, the API alert, its notification and
recovery. Cleanup traps restore the file, its permissions and the bridge state
if an assertion fails. These fault tests pause the bridge so automatic repairs
cannot hide the fault under observation.

## Holmes investigations

[HolmesGPT](../holmesGPT/README.md) uses the metrics, logs, traces and Linux state
to investigate alerts, follow separate SOPs and call guarded repair tools.
Investigation results and actions go to the separate ntfy `holmes` topic.

The demo runtime is isolated in `/opt/labstack-fastapi/venv`. Export its locked
dependencies from the repository on the Mac:

```sh
uv export --locked --only-group demo-fastapi --no-hashes --no-emit-project -o provision/observability/assets/requirements-fastapi.txt
```

They are not runtime dependencies of the Labstack CLI. Reprovisioning disables the
old Apache and Python API units before starting FastAPI on the public demo port.
