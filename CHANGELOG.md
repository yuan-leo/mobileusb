# Changelog

## 2.1.0

Add a live target-facing USB status panel, guarded force disconnect/present controls,
and drag-and-drop multi-file selection in the web manager. USB control actions remain
queue-based and are executed by the privileged controller; the web process never gains
root privileges. Force disconnect requires an explicit target-idle acknowledgement.
The controller now reports negotiated UDC speed/function details and distinguishes
queued changes from a genuinely attached target.


## 2.0.1

See [release notes](docs/releases/v2.0.1.md). Fix the web service's sibling bind
mounts so upload, Trash/restore and replacement operations preserve atomic rename
semantics. Add an existing-install repair script, reproducible release packaging,
and Linux mount-namespace regression coverage. USB synchronization is unchanged.

## 2.0.0 (initial repository snapshot)

Authenticated web file manager and conservative two-way USB reconciliation, with
incoming winning pathname conflicts and target edits preserved as duplicates.
