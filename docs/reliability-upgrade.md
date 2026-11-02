# Reliability upgrade and recovery

Target contract: desktop/Core 0.14.2, IPC 16, Core schema 23, build `schema23-skills-v1`. This document describes the migration contract; release acceptance is recorded separately. A source checkout or a passing mock UI test is not an accepted installer.

## Before upgrading

Close the desktop and its Core process. For an installation or downgrade exercise, use a separate Windows user or VM and synthetic data. Preserve a complete copy of the application data directory, including the database, attachment artifacts, settings, project authorizations and encrypted recovery store. Keep the original copy intact throughout acceptance. Do not include this directory in CI artifacts or bug reports.

## Database migration

The schema21 migration adds nullable `target_json`, `budget_policy_json` and `claim_token` columns to compaction attempts. Existing messages, attachment sources, checkpoints and audit references retain their identities. Old queued/running compactions become interrupted; no old claim grants permission to run again. Backfilled target identity contains only fields proven by the associated model run.

The schema22 migration adds `terminal.run` to the tool-operation table's allowed tools. It rebuilds the table transactionally, retaining operation identities, existing tool rows, indexes and review references, and checks foreign keys before commit.

The schema23 migration expands the same audited table for `skills.load`, `skills.read_resource` and `skills.save`, retaining rows, reviews and indexes. Skill registrations are stored separately in atomic `skills.json`; managed packages and revision backups stay beside the Core database.

For an existing supported database below schema23, Core first uses SQLite's backup API to create a uniquely named sibling file:

`<database filename>.pre-schema23-<unique ID>.bak`

The backup contains committed WAL changes and precedes migration. Earlier pre-schema21 backups remain intact. Reopening a schema23 database does not repeat that backup. The migration runs transactionally, updates the schema version and checks database integrity before commit. A migration error must be investigated with the database and backup preserved.

This automatic backup covers the Core database. It is not a replacement for the complete application-data backup: attachment files and Host authorization/recovery stores live outside SQLite.

## Runtime recovery

An interrupted or failed compaction requires an explicit retry. A permitted replacement model can be selected; its current capabilities and project policy are rechecked. A cancelled user request must not resume automatically on startup or provider configuration changes.

Terminal commands execute PowerShell with the current Windows user's privileges. The project-relative working directory is checked, but shell code is not a filesystem sandbox. Open mode executes directly; Human and AiReview modes require an actual user decision for each operation. A cancelled, timed-out or interrupted command is never automatically replayed. Finite commands and their children are cleaned up together, and bounded output and actual exit codes remain available in the conversation audit after reload.

File recovery uses Host journal version1 and snapshot version2. Existing snapshot version1 records remain readable, but they lack the object identity evidence required for safe undo; the UI explains why undo is unavailable. An uncertain file operation requires explicit recovery review and blocks further file changes in that project, including approvals prepared before the failure. It is never blindly replayed. A rollback conflict preserves externally changed files and reports affected relative paths. Preserve the recovery materials until that conflict is resolved.

This version retains journal-linked recovery snapshots, including completed records. Their storage counts against the 2 GiB recovery limit. If that limit is reached, new mutations fail before their first file write. Automatic retirement of completed journals is not implemented; do not manually delete unresolved journal or snapshot files to bypass the limit.

## Downgrading

Do not open schema23 data with an older binary. Keep the upgraded data and any work created since the upgrade as a separate intact copy. Restore an independently verified, complete pre-upgrade backup into the isolated test environment, then start the matching older binary and verify its data and authorization state.

Never automatically overwrite upgraded data with the `.bak` database. Restoring SQLite alone can mismatch attachment artifacts, current Host journals or authorizations. If only the automatic database backup exists, retain all current files and resolve the version mismatch before attempting a downgrade.

## Acceptance evidence

The first verified-handle backend supports Windows local-disk project roots. UNC/network roots fail closed with `file_access_root_unsupported`; the saved grant is retained. Hard-linked files and ambiguous `~` aliases are also conservatively rejected. Supporting network roots requires a separately validated backend; no hard network-I/O timeout is claimed.

Record database content/reference preservation and foreign-key checks, frozen Sidecar identity, desktop/Core startup, fresh installation, supported upgrade, and uninstall preserving application data separately. Record local and online provider compatibility separately from fake-adapter tests. Unperformed checks remain unverified; no automatic upload, tag or publication is part of this repair plan.
