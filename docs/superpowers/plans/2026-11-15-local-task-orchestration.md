# Local task orchestration implementation plan

Approved specification is the user's local-agent general orchestration and document workflow plan.

## Goal and architecture

Persist the user's goal before inference. Combine a revisable business plan with the existing audited model/tool execution loop. Expose document review as a composable tool. Verify coverage and published artifacts before completing a task.

## Constraints

- All authorized local tasks, one main model instance, sequential child work by default.
- Existing project authorization, offline terminal isolation, evidence checks and updater transport remain authoritative.
- Local task planning and discovery use existing native/structured tool routes.
- Schema34, IPC27, tool-contract-v6, settings20, worker-v2, version0.17.0-preview.2.
- Existing task monitoring/recovery and identity caches are reused.
- Real Qwen3-1.7B Q8 professional acceptance precedes publication. RTX3090 remains separately pending.

## Tasks

1. Persist goal/plan revisions and work-item coverage, expose task snapshots and completion checks.
2. Add capability discovery and plan-update tools to the existing loop, refresh available tools after discovery, preserve tool authorization.
3. Add complete text inventory and composable target review, remove generic retrieval admission blocking document review, reuse validated checkpoints.
4. Integrate goal creation and first-action planning, composite review/fill/export, result verification and document-wide consistency review.
5. Extend task card/diagnostics, migrate contracts and update product/build identity.
6. Full automated regression, independent review, actual Q8/document validation and verified release assets.

Each task follows RED→GREEN verification and records results in this plan's ignored ledger.

## Interfaces

- Business task service binds run/intent/root task identity, records atomic plan revisions and scoped work items.
- tools.discover returns authorized local tool descriptions and enables their registered definitions for later rounds.
- task.update_plan updates goal-derived steps, dependencies and success criteria.
- document.review_targets accepts a managed template and selected stable targets, returns coverage/report/artifact references.
- execution.request extends status and adds paginated plan/work-item reads.
- Completion differentiates confirmed deliverables, documented information gaps, and technical failure.

## Acceptance

Greeting skips RAG. Equivalent review wording and unknown classifications retain an executable path. Full/partial/multiple-document and review→revise→save tasks compose. Every enumerated source position has an outcome. Missing evidence remains a reported gap. Stale identities, cancellation and restart reconcile existing operations. File success follows verified publication. All request definitions/content fit the same context budget. Python/frontend/browser/Rust/migration/contracts/release checks pass. Q8 real output and DOCX rendering are checked before publication.
