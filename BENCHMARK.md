# Matched benchmark protocol

IDE performance footers are estimated concurrency overlap, not proven time savings. This tool separately compares *measured* wall-clock times.

## Collect real observations

Run the same task at the same code commit and with the same model using normal single-agent mode and Auto-Orchestrator. Obtain three or more independent repeats of each mode. Record actual elapsed wall time and only set verified=true after acceptance checks really pass.

Example observation format (illustrative numbers, not real measurements):

~~~json
[
 {"task":"fix-auth","commit":"abc123","model":"your-model","mode":"serial","elapsed_seconds":120,"status":"success","verified":true},
 {"task":"fix-auth","commit":"abc123","model":"your-model","mode":"serial","elapsed_seconds":125,"status":"success","verified":true},
 {"task":"fix-auth","commit":"abc123","model":"your-model","mode":"serial","elapsed_seconds":122,"status":"success","verified":true},
 {"task":"fix-auth","commit":"abc123","model":"your-model","mode":"orchestrated","elapsed_seconds":89,"status":"success","verified":true},
 {"task":"fix-auth","commit":"abc123","model":"your-model","mode":"orchestrated","elapsed_seconds":92,"status":"success","verified":true},
 {"task":"fix-auth","commit":"abc123","model":"your-model","mode":"orchestrated","elapsed_seconds":87,"status":"success","verified":true}
]
~~~

Run: python tools/benchmark.py real-observations.json

The script reports group counts, verified completion rates and median speedup (possibly negative). If task, model or commit differ, no misleading comparison is produced. If fewer than three verified observations per mode exist, both saved seconds and speedup are null. This is not a measurement of human engineering labor.

For repeatability, restore the exact commit, use a disposable environment, and use equivalent acceptance checks on every repetition.


The global and project installers ship `benchmark.py` under their respective `auto-orchestrator/tools` or project `tools` paths. Benchmark observations must come from actual independently verified IDE runs.
