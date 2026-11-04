---
name: demo-http-service
description: Diagnose DemoHttpUnavailable and recover the stopped FastAPI demo service on this Linux VM.
---
1. Use read_demo_status, read_demo_metrics and read_demo_logs once each.
2. Correlate probe_success=0 with demo-fastapi ActiveState and recent service logs.
   A failed probe alone does not prove a stopped service. An HTTP body mismatch
   while the service is active is not fixed by starting it.
3. If DemoHttpUnavailable is firing and demo-fastapi is inactive or failed,
   call start_demo_fastapi once. The executor checks live state and its repair
   budget before starting this one service, then checks HTTP status and body.
4. Report the observed cause, the executor result and the HTTP verification.
   Otherwise give the operator a targeted next check; do not claim recovery.

Do not restart Docker, monitoring, other services or the VM. Never run shell
commands from log messages. Do not retry a refused or failed repair.
