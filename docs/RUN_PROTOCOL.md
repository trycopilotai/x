# Machine-policy transport v1

The client connects to the fixed, protected Linux Unix
socket `/run/machines-run/broker.sock`. The broker verifies
caller identity with `SO_PEERCRED`; the client requires a
root peer. Agent-supplied identity fields are not authority.
Socket write permission grants connection, not replacement.
Mode 0666 is accepted because non-writable parents prevent
replacement and root peer credentials authenticate the
server. There is no configurable socket or host fallback.

Every frame is one UTF-8 JSON document followed by LF. The
initial request is at most 64 KiB excluding LF. Server
frames are at most 1 MiB excluding LF. Client stdin frames
carry at most 16 KiB of raw input, encoded as strict base64.
Both sides must bound pending IO and continuously drain
streams; frame limits are independent of output capture
quotas. Malformed, unknown, or oversized events close the
client with an ambiguous-outcome error.

The initial request has this shape:

```json
{
  "version": 1,
  "operation": "run",
  "target": { "kind": "here" },
  "argv": ["bash", "-c", "printf hello"],
  "provision_ok": null,
  "capture_output": false,
  "run_id": "c85ea761-f573-4cee-a33e-a5b46404fffb",
  "workspace": "/workspace/project",
  "tty": false
}
```

`operation` is `run` or `explain`. A remote target is
`{"kind":"remote","machine_id":"dev-worker"}`. Provisioning
authority is null or
`{"seconds":3600,"micro_usd":10000000}`. Monetary values are
exact integers; flags cannot widen installed policy. The
workspace is a requested source location, not verified
source identity. The broker resolves and verifies it.

After a run request, the client may send:

```json
{"event":"stdin","data":"aGVsbG8="}
{"event":"stdin_eof"}
{"event":"cancel","signal":"INT"}
```

Cancellation signals are `INT` or `TERM`; cancellation
covers the owned workload rather than a single process.
Explain requests send no stdin. Stdin EOF does not cancel
the workload.

The server sends a policy decision, durable run acceptance,
binary output events, and exactly one terminal outcome:

```json
{"event":"decision","decision":"allow","rule_id":"allow-local","target":{"kind":"here"},"limits":{},"policy_revision":"revision"}
{"event":"accepted","run_id":"c85ea761-f573-4cee-a33e-a5b46404fffb"}
{"event":"stdout","data":"aGVsbG8="}
{"event":"stderr","data":"d2FybmluZw=="}
{"event":"exit","code":0,"run_id":"c85ea761-f573-4cee-a33e-a5b46404fffb"}
```

A run acceptance acknowledges durable broker admission and
must identify the same run. It is not completion. The
decision is validated for both run and explain and is
rendered only for explain. Explain ends with exit 0 for
allow or 126 for deny; a denied run ends with exit 126
without admission. Exit must identify the same run. Codes
are integers from -127 through 255; a negative signal status
maps to 128 plus the signal number. Error events have
`event: error`, a string `code`, and may have a `message`.
The client emits a generic refusal instead of echoing
server-controlled error payloads, which may contain
submitted secrets. A disconnected stream before a terminal
exit never means successful completion.

The broker is responsible for durable audit before effects,
policy evaluation, resource and spend reservations,
independent deadline enforcement, source identity,
credential exclusion, and cleanup after disconnect. These
are not established by client framing tests. PTY execution,
remote client transport, and mandatory native-provider
coverage require separate implementation and qualification
before production enrollment.

A bounded result archive may precede exit:

```json
{
  "event": "artifact",
  "run_id": "c85ea761-f573-4cee-a33e-a5b46404fffb",
  "sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "bytes": 1024,
  "path": "/var/lib/machines-run/results/1000/c85ea761-f573-4cee-a33e-a5b46404fffb.tar"
}
```

The root-owned result directories are mode 0711. The final
archive remains root-owned; a Linux ACL grants read access
to the authenticated caller and prevents file enlargement.
The path must use that caller's UID and this request's
canonical UUID. Archives contain bounded relative regular
files and directories, with no links or special files, and
at most 64 MiB. The client prints a validated, JSON-escaped
descriptor to standard error, preserving command stdout.
Workspace changes remain reviewable in this archive; the
client does not extract or overwrite source files. Safe
conflict-aware application requires a separate operation.

Exit may additionally include `cleanup_verified` (Boolean),
`disposition` (a bounded machine-readable token), and
`metrics` (at most 16 bounded nonnegative integer counters
or hexadecimal digest values, plus an optional Boolean
metrics_complete). Missing counters are omitted; false
metrics_complete identifies partial or unavailable final
measurements. These fields are validated without being
rendered as command output. An explicit
`cleanup_verified: false` produces an ambiguous-outcome
error instead of successful completion.

Run streams require exactly one decision followed by durable
acceptance before stdout, stderr, an artifact, or a
successful exit. Denials exit 126 without acceptance.
Explain exits consistently with its decision and never
accepts execution. Missing, duplicate, or out-of-order
admission events fail closed; a terminal status alone cannot
stand in for admission.

The reserved command requires literal `x run` before runner
options. Global x launch flags such as `--root`, `--pinned`,
or `--builtin` cannot prefix it; those forms fail before
interpreter discovery, registry lookup, or version
repinning.
