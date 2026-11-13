# Task 2 implementation report

Implemented standalone `document.read_attachment`, appended after existing tools and definitions. Read quota, retry read-only classification and capability quota status include the tool.

`DocumentAttachments.read(attachment, *, offset=0, limit=20, physical_page=None, section=None, max_characters=16000, progress=None)` returns JSON-friendly `attachment_id`, `source_sha256`, `trust`, `text_artifact`, `blocks`, `next_offset`, `total`, and `warnings`. Blocks contain text, structured locator, character_offset. Generic locators receive stable block IDs. Long blocks split into bounded chunks and pagination continues using next_offset with the same filters/budget. Serialized Unicode JSON is checked against the caller character budget, with controlled budget errors when metadata cannot fit.

Original source hash is validated each read. Text artifacts are generated internally, then path-contained and hash/size verified before reuse. Caller artifacts are ignored. Cached text_artifact metadata is returned on every page, with unchanged artifact_id on cache hit. Runtime integration should persist only artifact IDs not already present. Parsing warnings, including missing OCR, remain visible.

Built-in source-mode cache identity covers parser command/limits, parser and OCR Python component contents, OCR availability/runtime command, fully verified model profile digests, CPU and thread configuration. Unknown custom/frozen identities conservatively reparse. Custom components can opt into cache through complete immutable cache_identity values. Cache is service-local. OCR model file verification happens on each identity computation.

Validation used the established external venv Python and elevated environment with TEMP/TMP and pytest basetemp under F:/agent-interaction-v0163-20261113.

- Initial focused plus existing extraction run yielded 19 passed and one migration-version assertion failure, expected 30 versus root integration version 31.
- Final command `python -m pytest services/agent-core/tests/test_document_attachment_read.py services/agent-core/tests/test_document_attachments.py -q -k 'not schema26_upgrade' --basetemp=F:/agent-interaction-v0163-20261113/task2-tests-final` yielded 20 passed, 1 deselected in 3.73s.
- Regression coverage includes page/section filtering, generic locators, long-block pagination, original mutation, text artifact tampering, parser/OCR identity changes, unknown configuration reparsing, caller artifact rejection, budget errors, and malformed tool arguments.

Implementation preceded new test execution in this task, so strict red-first TDD was not achieved. Root owns schema-version migration test adjustment and runtime authorization/persistence integration.

## Review fix round 1

Addressed both P2 findings and the production frozen paging ruling. Warning metadata now comes only from the generated preamble before the first SOURCE marker. Literal WARNING-prefixed document content stays in text blocks, while genuine extraction/OCR warnings remain present.

Built-in source and frozen identities now hash the actual Core/interpreter executable. Available OCR runtime reuse requires its runtime.json manifest and every manifest file to pass existing verify_runtime verification on every read. The verified manifest digest enters the identity. Same-path executable/dependency replacements invalidate cached extraction when the manifest is updated. File tampering against an unchanged manifest disables cache reuse. Frozen built-in mode can reuse its artifact across continuation reads. Custom identities still have their explicit contract, and unknown/unverifiable runtime identities conservatively reparse. Source-mode installations without a verifiable runtime manifest conservatively reparse when OCR is available.

Red-first regressions for literal WARNING source lines and source/frozen runtime cache behavior initially produced 3 failures. After implementation, the final focused command using the established elevated venv environment was `python -m pytest services/agent-core/tests/test_document_attachment_read.py -q --basetemp=F:/agent-interaction-v0163-20261113/task2-round1-final`, yielding 11 passed in 2.16s. Coverage verifies unchanged cached artifact IDs on continuation, same-path OCR executable and dependency mutation, Core executable mutation, and unverified runtime file mutation.

Combined extraction tests yielded 23 passed and one failure in test_normal_chat_entry_extracts_document_before_model_dispatch, whose eager-extraction expectation conflicts with root's on-demand runtime update. Root was informed and owns that test/integration adjustment. No unrelated runtime paths were edited.
