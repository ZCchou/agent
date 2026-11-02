# agent Desktop

Local-first desktop client built with React, Tauri and the Python Agent Core.

## Windows development

Prerequisites: Node.js 24.14.0, Rust 1.98.1/MSVC with `rustfmt` and `clippy`, Conda, and the `agent` environment with Python 3.12. The repository's `rust-toolchain.toml` selects the Rust version used by CI and release packaging.

Install dependencies from the repository root:

```powershell
conda activate agent
python -m pip install -r services/agent-core/requirements-build.lock
python -m pip install --no-build-isolation --no-deps -e "services/agent-core[dev]"
cd apps/agent-desktop
npm ci
```

Return to the repository root and build the Python sidecar whenever Core code changes:

```powershell
.\tools\build-python-sidecar.bat
.\tools\verify-python-sidecar.ps1
```

Close this worktree's running preview and let its Sidecar exit before rebuilding an executable it is using. Do not stop unrelated instances or remove application data. The builder installs locked Python dependencies; do not run it while another task is using the same Conda environment.

Then launch the desktop application:

```powershell
cd apps/agent-desktop
npm run tauri dev
```

The Tauri production build verifies that the target-specific Sidecar exists and matches the Host contract. It deliberately does not rebuild Python implicitly during ordinary frontend builds.

## Windows release package

From the repository root, run the complete release pipeline:

```powershell
.\tools\release-windows.bat
```

The pipeline installs locked dependencies, rebuilds and verifies the Python Sidecar, runs the Python/React/Playwright/Rust quality gates, creates a current-user NSIS installer, and writes a release manifest plus SHA-256 checksums under:

```text
.artifacts/releases/v<version>/windows-x86_64/
```

For a local packaging-only check after the gates have already passed:

```powershell
.\tools\release-windows.bat -SkipTests -SkipDependencyInstall -AllowDirty
```

Unsigned builds are appropriate for internal testing. Official builds provide the certificate thumbprint through `AGENT_WINDOWS_CERTIFICATE_THUMBPRINT`; certificates and private keys are never stored in the repository. See [Windows releasing](../../docs/releasing-windows.md) for the full contract.

## Local data and recovery

The current Core database schema is v23. Supported databases are migrated in place and checked
for foreign-key and manual-order integrity before the Core starts. Do not delete
`agent.db` to upgrade. Legacy schema-v1 development backups are intentionally not
imported and are removed only after the v4 database passes validation.

Deleting a standard chat, project work item, or project moves it to the Trash section in
Settings for 30 days. Trashed project directory grants are retained but suspended; they
become active again only if the project is restored.

The product has two conversation tracks:

- Standard chats can be organized into chat groups and never receive computer tools.
- Project work inherits its project's behavior category and may use one explicitly
  authorized directory.

Project work accepts ordinary task descriptions and can list, search, read, create,
edit, copy, move, rename, and delete files inside its explicitly authorized project
directory. The approval mode can be set per work item to Human approval, AI review, or
Open (project only). Fixed boundaries always reject paths outside the project, `.git`
internals, application data, credentials, and directory-link escapes. Recoverable
changes create encrypted recovery points. Standard chats never receive file tools.

Terminal commands execute as the current Windows user after authorization. Their
working directory is validated, but commands are not restricted to that directory
by an OS sandbox and may access the network. Review commands before approval.
Application control and multi-agent execution are not provided.

## Quality gates

Build and verify the real Sidecar from the repository root before Host tests. Run the following groups from the named directories; stop on a failed command. See [isolated local checks](../../docs/releasing-windows.md#local-offline-checks) for temporary data and build directories.

In `apps/agent-desktop` (Playwright uses Microsoft Edge; run `npx playwright install msedge` first if it is missing):

```powershell
npm run check
```

In `apps/agent-desktop/src-tauri`:

```powershell
cargo fmt --all -- --check
cargo test --locked
cargo clippy --locked --all-targets --all-features -- -D warnings
```

In `services/agent-core`:

```powershell
conda run -n agent python -m pytest
conda run -n agent python evals/context/run_context_eval.py --verify-mutations
```

PR CI runs `core-check`, `frontend-check`, and `windows-host-check` with read-only repository permissions and no live-model or signing secrets. Remote branch protection and failure-injection verification remain pending; workflow files alone do not enforce merge gates. Mock UI tests, frozen Sidecar checks, and NSIS smoke tests do not establish real desktop startup, preservation during an upgrade, or live-model compatibility. Those require the separate [release acceptance evidence](../../docs/releasing-windows.md#installer-and-live-model-acceptance).

Core 0.14.2 uses IPC v16 and build ID `schema23-skills-v1`. The Host rejects a
stale Sidecar instead of presenting it as healthy. Context snapshots store source IDs,
hashes, trust metadata, token estimates, and per-provider-call rounds; message and file
bodies remain in their authoritative stores rather than being duplicated into audit rows.

The generated Sidecar executable and local SQLite database are build/runtime artifacts and are not committed.
