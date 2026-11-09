# Preview release and incremental update plan

User request is to test, commit, upload, package and provide an incremental update.

## Delivery decisions

- Publish `v0.15.0-ocr-preview.2` as a prerelease on the existing preview branch.
- The public installer and public delta retain `io.github.zcchou.agent.ocrpreview` and its data directory.
- Provide a separate delta for the local Document Preview schema27 build, retaining its application identity.
- Reuse the six previous runtime archives only after exact payload-manifest equality and archive hash verification. Rename the release assets for version2.
- Deltas validate the exact previous three application hashes and runtime manifests. They stage replacements, retain backups and recover interrupted attempts. User data is migrated by Core on its next start.
- Record the unsigned preview build, cached OCR acceptance, slow local retrieval and the observed ordinary-answer quote failure in the release notes.

## Implementation

1. Run full Python, frontend, browser and Rust regression suites.
2. Extend `tools/ocr_preview_release.py` with a validated preview version and verified archive reuse. Pin behavior in `tools/tests/test_ocr_preview_release.py`.
3. Add `tools/preview_delta.py` and `tools/installer/update-preview-application.ps1`. Test valid updates, corrupted input, wrong baseline, partial replacement rollback, interrupted recovery, idempotence and unsafe paths in `tools/tests/test_preview_delta.py`.
4. Build the OCR-identity Host from the same application source and packaged Core. Preserve the tested Document Preview binaries separately.
5. Build the offline installer, shared runtime archives and two delta ZIPs. Exercise updates in isolated directories and validate packaged Core IPC and schema migration.
6. Perform a fresh packaging review, commit the release tooling and documentation, push the preview branch, create the prerelease and upload verified assets.
7. Re-read the published asset list and hashes, then provide download links and baseline-specific instructions.

## Verification focus

Wrong application identity, busy processes, concurrent installers, file corruption, path reparse points, interruption during replacement, database/binary rollback pairing and private file exclusion must remain explicit.
