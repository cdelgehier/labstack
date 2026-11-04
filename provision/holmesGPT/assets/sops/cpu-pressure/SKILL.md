---
name: demo-cpu-pressure
description: Investigate DemoCpuBusy and stop only the identified demo CPU stress unit when it is responsible for current CPU saturation.
---
1. Call read_demo_metrics, read_demo_status and read_demo_logs once each.
2. Confirm DemoCpuBusy and report host_cpu_busy_percent from the metrics tool.
   This is already the percentage of BUSY CPU over 20 seconds. The raw query
   1 - avg(rate(...idle...)) is also BUSY, not idle: do not invert it again.
   A missing value is unknown evidence. If CPU use has fallen, report that.
3. Use the structured cpu_stress fields for the demo job's identity and CPU
   use. The process snapshot can suggest other candidates when this job is not
   confirmed, but its process %CPU is a lifetime average, not the host window.
   Do not invent PIDs or claim a culprit from that snapshot alone.
4. Report whether FastAPI and its probes are still healthy. Use recent logs
   only when they help explain this incident; old errors are not its cause.
5. In the cpu_stress status, verify state=active, command_matches=true and a
   nonempty invocation_id. Its cpu_percent measures the share of total VM CPU
   over one second. Require at least 50%, along with host CPU still above 85%
   and DemoCpuBusy firing. Other busy processes alone do not authorize a stop.
6. Only when all conditions agree, call stop_demo_cpu_stress once. The executor
   rechecks the job identity and stops only labstack-demo-cpu.service and its
   child processes. It verifies host CPU below 85% and the alert's resolution.
7. Report the exact executor result. If evidence is missing, the unit expired,
   the job changed or another process is responsible, request human inspection
   of that process and its service. A refused or failed repair must not be retried.

Never stop the memory stress unit, an arbitrary PID, Grafana or another service.
Do not change priorities or reboot. Without a verified executor result, do not
claim recovery; report the missing check and the required human intervention.
