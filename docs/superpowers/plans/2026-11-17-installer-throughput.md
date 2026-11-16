# Installer throughput implementation plan

> For agentic workers: use executing-plans task by task.

**Goal:** Reduce verified complete offline installation/update time while preserving ownership, hash checks and crash recovery.

**Architecture:** Keep preview.6 application/Core/schema unchanged. Optimize only the native installer helper and rebuild an independent installer revision; reuse identical signed program archive and runtime volumes. Preserve the original preview.6 package. No public upload or merge.

**Tech stack:** Existing Rust, Win32 filesystem handles, JSONL transaction journal and NSIS.

## Evidence and design

Measured same-device baseline: first install 917.903 seconds, same-version reuse545.678 seconds, uninstall130.015 seconds. Initial reuse SHA scans248.7s and241.8s; publication329.2s with a durable journal flush per file; cleanup81.1s/53.1s enumerates every next-manifest stage path even when absent.

- Phase-scoped directory validation uses open Windows directory handles without delete sharing. Directory handles prevent rename/replacement until scope ends; live handle attributes are still checked on EVERY lookup, because attribute-only writers can set a reparse point despite share restrictions. Leaf files remain checked on every operation. No cache persists across installer operations. Drop scope before all publication/namespace renames and removal. Cache only path reopening, never validation results.
- Skip the second full reuse scan only when there are no file mutations; keep the initial full hashes, ownership checks and postflight. Updates that mutate files retain prepublication verification.
- Write a bounded batch of intent lines and sync before any file in that batch moves. Keep each old file open against writes/deletion from its hash until immediately before backup; recovery already ignores incomplete final lines and safely processes unexecuted intents; test crash at batch boundaries and partially executed batches. Keep data sync and write-through renames.
- New journals record exact staged paths. For committed transactions, retain directory ownership and controls but do not scan nonexistent staged files; older journal compatibility remains conservative. Unknown content is preserved.

## Review focus

- Parent junction replacement during a cached phase must be blocked; leaf reparse/hardlink rejected.
- Pins must be released before transaction directory moves and uninstall.
- Crash before/after a journal batch flush must restore exactly the old state; already committed operations stay committed.
- A missing/corrupt reused file must trigger repair; no-op still executes postflight and propagates errors.
- Sparse cleanup cannot remove unknown files, nor leave owned empty stage directories behind.

## Tasks

- [x] Add failing no-op, exact cleanup and batch-boundary regressions; measure parent checks with actual handles.
- [x] Implement phase-scoped parent pins and existing-file validation; add mutation tests.
- [x] Implement no-op scan avoidance, exact cleanup and durable batched publication.
- [x] Run native fault/path/ownership/process tests and Rust checks in parallel where independent.
- [x] Same-device complete install/reuse/uninstall comparison with exact asset hashes.
- [ ] Commit/push source on a separate branch without merge, rebuild local installer revision, verify contents/signatures and deliver reports. No installer upload.

Application professional-review/27B/3090 acceptance remains pending; installer optimization does not change that status.
