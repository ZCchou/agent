# Windows releases

This project uses the public repository `ZCchou/agent` for both source and distribution. Source checkout alone includes no production updater signing identity.

## Prerequisites

Use Windows, Node.js 24.14.0, Rust 1.98.1/MSVC and the `agent` Conda environment with Python 3.12. Install the locked dependencies described in the root README. Microsoft Edge is required for UI tests.

The unified application has an independent updater public key in `apps/agent-desktop/src-tauri/tauri.conf.json`. Keep the matching private key outside the repository. The program release tool signs the program ZIP and channel feed and verifies the public key before publication. CI publishing requires separately provisioning the matching key through protected secrets. Never commit private keys.

Unified feeds are `https://raw.githubusercontent.com/ZCchou/agent/main/updates/airworthiness-preview.json` and `airworthiness-stable.json`, with detached `.sig` files. Use `tools/program_release.py` to produce signed program-only updates. Each package includes Host, Core, the application DLL and updater helper. Inference components and model weights are maintained through full offline delivery or local import. Legacy release tooling below remains available for its corresponding package format.

## Local packaging

Run `.\tools\release-windows.bat` from the root. This builds and verifies the Python sidecar, runs quality gates and builds an NSIS installer. `-InstallerSmokeTest` adds installer checks. A local unsigned packaging check can use `-SkipTests -SkipDependencyInstall -AllowDirty` only after separately passing those checks; it is not an official release. Authenticode signing uses `AGENT_WINDOWS_CERTIFICATE_THUMBPRINT` and remains separate from updater signing.

Artifacts and manifests are written beneath `.artifacts/releases/v<version>/windows-x86_64/`. The canonical distributable installer is `agent-<version>-windows-x86_64-setup.exe`.

The default installer includes the complete CUDA-capable inference runtime, which also supports CPU execution. Users can choose CPU or NVIDIA CUDA in the application, with CPU as the default. Runtime files travel inside the installer and portable release. Prepare the runtime on the build machine with `tools/build-rag-runtime.ps1 -Device cuda`, or supply an existing verified directory using `-RagRuntimeDirectory`. First-time dependency preparation belongs to the build machine. The target computer needs a compatible NVIDIA driver for CUDA execution.

Use `-RagRuntimeDevice cpu` to build the smaller CPU-only edition. The full edition rejects a CPU-only runtime during staging and portable packaging.

## Official workflow and publication

The release workflow is manually dispatched and uploads verified artifacts. It does not automatically create a release or tag. Keep a new release tag unused until publication.

```powershell
gh workflow run windows-release.yml --repo ZCchou/agent --ref main -f release_tag=v0.14.3-preview.1 -f update_channel=preview
```

After the workflow succeeds, download its artifacts. The `tools/release_windows.py publish-public` operation takes `--directory`, `--config` and `--workflow-run`. It validates signatures, manifests, the public repository, and the successful workflow commit before creating a draft release at that exact source commit. It uploads and verifies asset sizes and digests before publishing, then advances only the chosen signed channel feed atomically. Existing tags/releases and equal or older channel versions are rejected.

Use `prepare-public` and `validate-public` with the same directory/config arguments for offline package preparation and inspection. Do not distribute diagnostic sidecars, private keys or unfiltered build output. Review the public directory before publication.

## Local offline checks

Run the commands in the root README with test-specific `TEMP`, `TMP`, `AGENT_DATA_DIR` and `CARGO_TARGET_DIR` paths. Rebuild the sidecar before Host tests. CI defines `core-check`, `frontend-check` and `windows-host-check`; required branch checks must be configured separately in GitHub.

## Installer and live-model acceptance

Mock UI tests and sidecar protocol checks do not establish actual desktop startup, safe upgrade behavior or provider compatibility. Before distributing an official installer, verify install/uninstall and restart on Windows, preservation of this application's own data across upgrades, approval behavior, update signature rejection and the intended live provider workflows. Record results without credentials or private content.
