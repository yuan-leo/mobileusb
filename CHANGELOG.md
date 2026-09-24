# Changelog

## 2.0.1

See [release notes](docs/releases/v2.0.1.md). Fix the web service's sibling bind
mounts so upload, Trash/restore and replacement operations preserve atomic rename
semantics. Add an existing-install repair script, reproducible release packaging,
and Linux mount-namespace regression coverage. USB synchronization is unchanged.

## 2.0.0 (initial repository snapshot)

Authenticated web file manager and conservative two-way USB reconciliation, with
incoming winning pathname conflicts and target edits preserved as duplicates.
