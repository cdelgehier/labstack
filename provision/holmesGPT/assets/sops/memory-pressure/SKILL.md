---
name: demo-memory-pressure
description: Investigate DemoMemoryLow using available RAM and recent OOM evidence, then request human intervention without stopping workloads.
---
1. Call read_demo_metrics, read_demo_status and read_demo_logs once each.
2. Confirm DemoMemoryLow and report MemAvailable / MemTotal as a percentage.
   This metric includes reclaimable cache. Do not infer low memory from low
   free memory alone. Missing metrics mean the current pressure is unknown.
3. Check recent kernel logs for actual OOM events. Low available RAM does not
   prove an OOM kill; only name a victim when the returned kernel evidence
   identifies it. Do not claim there was no OOM outside the bounded log window.
4. The process snapshot is sorted by CPU, not memory. Its %MEM values may show
   candidates, but cannot prove which process holds the most RAM. If needed,
   ask an operator for ps -eo pid,comm,rss,pmem --sort=-rss and its service limits.
5. Report FastAPI and probe health, the observed pressure and any proven OOM.
   If the operator confirms the demo memory job, propose task restore:memory.
   If available RAM has recovered, report that without inventing a repair.

No automatic repair is authorized. Never kill processes, drop caches, change
memory limits, restart services or reboot. State any missing attribution evidence,
the next human check and that no repair was executed.
