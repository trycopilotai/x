# Example provenance

Documentation snippets, unit-test inputs, and command-demo
inputs are synthetic fixtures. They are illustrative
repository layouts, commands and session-store records, not
records taken from a customer's repository or session.

The files under `evidence/fixtures/` retain the synthetic
configuration before and after the recorded agent exercise.
`scripts/record_session.py` defines the synthetic inputs to
the separate scripted command-line demonstration.

The transcripts retain observed output. The Codex event
streams record actual agent actions against the synthetic
fixture; local absolute paths were replaced as declared in
`evidence/agent-manifest.json`. The scripted command-line
transcript is unedited, as declared in
`evidence/demo-manifest.json`. Synthetic inputs do not mean
that these observed outputs or agent actions were invented.
