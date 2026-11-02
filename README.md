# agent

<img src="apps/agent-desktop/src/assets/agent-logo.svg" width="72" height="72" alt="agent icon">

A local-first desktop agent built with React, TypeScript, Tauri 2, Rust and Python. Organize conversations and project work, connect model providers, inspect model routing, and use project files, skills, image inputs and web search.

## Development on Windows

Requires Node.js 24.14.0, Rust 1.98.1/MSVC, Visual Studio C++ build tools, Microsoft Edge and Conda with Python 3.12.

```powershell
conda create -n agent python=3.12 -y
conda activate agent
python -m pip install -r services/agent-core/requirements-build.lock
python -m pip install --no-build-isolation --no-deps -e "services/agent-core[dev]"
cd apps/agent-desktop
npm ci
cd ../..
.\tools\build-python-sidecar.bat
.\tools\verify-python-sidecar.ps1
cd apps/agent-desktop
npm run tauri dev
```

Frontend-only preview: run `npm run dev` in `apps/agent-desktop`. Native capabilities require the desktop Host and Python sidecar.

## Checks

```powershell
# From services/agent-core
python -m pytest
python evals/context/run_context_eval.py --verify-mutations
# From apps/agent-desktop
npm run check
# From apps/agent-desktop/src-tauri, after building the sidecar
cargo fmt --all -- --check
cargo test --locked
cargo clippy --locked --all-targets --all-features -- -D warnings
# From the repository root
python -m unittest discover -s tools/tests -v
```

## Data and security

The desktop application uses its own `io.github.zcchou.agent` application directory, `AGENT_*` environment variables, `agent.db` database and OS credential-store namespace. Provider credentials must be configured by each user. Local databases, logs, private keys and build artifacts are excluded from source control.

Project file tools validate authorized roots and create recovery points where supported. **Terminal commands run as the current Windows user; setting their working directory does not sandbox their filesystem or network access.** Review commands before approval. See [security guidance](SECURITY.md).

Automatic updates are disabled until an independent updater signing public key is configured. No production signing key is included. See [Windows releases](docs/releasing-windows.md) before publishing binaries. Source publication does not imply that installer acceptance or live-provider compatibility has been verified.

## Layout

- `apps/agent-desktop`: React UI and Tauri Host.
- `services/agent-core`: Python runtime, storage, routing and tool orchestration.
- `contracts`: shared protocol and reliability fixtures.
- `tools`: sidecar building, verification and release tools.

MIT licensed. Dependencies retain their respective licenses.
