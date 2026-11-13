# Task 1B Host and frontend report

## Integration

- `TauriModelRunRepository.start` passes `intent_async` and `intent_scope` through the existing `start_model_run` Host boundary. Its promise retains the existing accepted/preflight-rejected result union and remains pending while clarification or manual retry is required. Direct legacy responses remain supported.
- `intentTasks.ts` owns polling, scoped restoration, revision checks, duplicate action exclusion, acceptance waiters, cancellation before initial task registration, and recovery adoption tracking. Active tasks poll every 750 ms. Waiting and terminal tasks stop polling. Recovery selects the newest scoped task and ignores a list response superseded by a new send.
- `IntentTaskDialog` uses the browser's native modal dialog for keyboard focus confinement and Escape behavior. It shows explicit clarification confirmation, suggested options and free text, technical timeout/retry notices, remaining budget, resource-specific model settings navigation, and inline resume entries after closing. Closing a deadline notice preserves the running deadline.
- ChatPage supplies the inspector scope, forwards pre-acceptance Stop to intent cancellation, and reloads the actual accepted conversation/run when recovery completes without the original send promise. Async recovery checks the captured scope and run identity before adoption.
- Host `intent_request` exposes list/status/respond/retry/extend/cancel. It validates the durable Host-normalized authorization before mutating an existing task and reconciles only accepted real runs into committed staging. A pre-start idempotency authorization closes the process-restart gap between Core start and its response.
- `AttachmentStore` persists intent authorizations atomically under its existing private attachment root, pins pending task files against discard, restores validated pending manifests/rejection receipts on Host restart, and releases pins on completion/cancellation. Acceptance checks compare original attachment IDs, manifests, provider/model selection, project and conversation against the authorized snapshot.

## Settings and progress

- Settings schema 19 adds `intent_timeout_seconds` with default 90 and accepted range 30–180.
- `main_model_idle_seconds` and `auxiliary_model_idle_seconds` default to 1800. Zero selects manual unload. Other values accept 60–86400 seconds. Both use the existing persisted routing configuration injection and live update command.
- Model settings provide 5/15/30/60 minute presets, custom 1–1440 minute input, and manual unload. The timeout shows a derived maximum of three attempts and three times the configured timeout. Copy explains immediate timer reset, active/pinned retention, and auxiliary memory-pressure reclamation.
- Document-processing activity recognizes localized `model_loading` and `reranking` counts.

## Verification

- Observed settings migration RED at schema 18 versus 19, then GREEN after schema/default/validation implementation.
- Observed attachment restart RED with `attachment_staging_expired`, then GREEN after pending manifest recovery.
- Observed duration parser RED assertions and stale restore RED, then GREEN after the corresponding implementations. Document progress RED returned null, then GREEN with model-loading/reranking support.
- Frontend full unit suite passed with 49 files and 216 tests using `npx vitest run src --configLoader runner`.
- TypeScript check passed using `npx tsc --noEmit`.
- Production Vite build passed using `npx vite build --configLoader runner`. Vite retains its existing large-chunk warning.
- Native library suite passed with 294 tests and two ignored native subprocess fixtures using `cargo test --lib -j 1` and the shared F-drive cache. Focused intent tests passed separately.
- Focused Edge browser suite passed with three tests covering explicit clarification confirmation, dismissal/resume, unchanged deadline after close, scope switching, small-window layout, and technical failure retry presentation.
- `git diff --check` passed. Rustfmt ran only on owned Rust sources.

## Environment and remaining integration notes

- Vite's default bundled configuration loader attempts to write into a linked dependency directory outside the writable workspace. Verification used `--configLoader runner`.
- Tool-owned transient Vite processes were unreachable after launch. A hidden persistent localhost-only Vite process fixed browser verification. It is PID 15712 on port 5197, with logs under `F:/agent-interaction-v0163-20261113/intent-vite*`. The temporary browser configuration and build log are under the ignored app `.artifacts` directory.
- Automatic approval review rejected an attempted diagnostic bind to all interfaces because it would expose workspace assets to the LAN. Verification completed with localhost binding.
- Root owns Core intent retries and residency runtime semantics. I reported that Core manual retry should return an active queued state immediately before launching, so frontend polling resumes reliably. Existing structured `preflight_rejected` results already flow through unchanged. Terminal errors preserve their code, message, and details on the rejected start promise.
- Full browser-suite execution and packaged native installer execution remain with root integration. This task's focused browser tests use a mocked Tauri boundary.


## Review round 1

- Fixed findings 1 and 4 from `task-1-review-report.md`.
- Confirmed terminal intent preparation rejects with an `intentTerminal` marker. ChatPage clears the matching pending send identity after confirmed cancellation/terminal failure or a completed preflight rejection. Uncertain admission errors preserve their identity for reconciliation.
- Resolved promises retain acceptance ownership until a live handler acknowledges adoption. Scope cleanup releases that ownership, including a scope change before the initial start response registers its task. Recovery consumes acceptance only after its scoped asynchronous adoption succeeds.
- Added a browser-only scope harness using the real ChatPage, repository, task store, AppProvider and surrounding providers. AppContext is exported to inject controlled navigation in the harness. The production New Chat action creates a new draft revision, so the fixture supplies explicit navigation back to the original draft for the requested recovery scenario.
- Observed RED in the real browser path for Stop followed by unchanged Send, terminal failure followed by unchanged Send, completion before returning to a draft, and completion after returning. The uncertain admission scenario passed before the fix and remains covered.
- The store acknowledgement assertion also reproduced RED because completion consumed acceptance before the scope handler adopted it.
- GREEN verification passed for `npx playwright test intent-send-recovery.spec.mjs intent-dialog.spec.mjs --config=.artifacts/intent-playwright.config.mjs --reporter=line --output=.artifacts/intent-recovery-tests` with `AGENT_UI_BASE_URL=http://127.0.0.1:5197`. All eight tests passed.
- `npx vitest run src --configLoader runner` passed 49 files and 220 tests. `npx tsc --noEmit` passed. `npx vite build --configLoader runner` passed with the existing large-chunk warning. `git diff --check -- apps/agent-desktop` passed.
- Browser tests emulate the Tauri command boundary and execute the actual frontend entry points. Core retry classification and persisted budget accounting are coordinated by root.


## Template progress completion

- Added localized counted labels for `template_planning` and `template_filling` in the shared document-processing formatter. Counts use Core's `completed` and `total` values.
- AssistantRunMessage already displays elapsed duration alongside every activity label, so these labels use that existing duration display.
- English and Chinese template-label tests reproduced RED with null output and then passed after phase mapping/localization.
- Full frontend unit verification passed 49 files and 222 tests using `npx vitest run src --configLoader runner`. The focused document progress suite passed five tests. `npx tsc --noEmit` and scoped `git diff --check` passed.
- Existing browser integration forwards `run.document_processing` through this formatter. Root's full browser verification continues on its existing server.
