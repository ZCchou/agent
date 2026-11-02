# Context Engine v2 evaluation

This is the offline retention, source, attachment selection, and recovery gate
for `context-default-v1`. It does not measure model answer or memory quality.

- `cases/manifest.json` contains 54 cases with explicit expected facts, source
  aliases, excluded sources, selected ranges, errors, and terminal statuses.
  Every excluded source is a real row in a sibling work or another project.
  The original 48 cases now check retained historical facts; six additional
  sentinels cover corrected decisions/preferences, unfinished work, compressed
  provenance, selected excerpts, untrusted instructions, small contexts, and
  provider failure cleanup (some sentinels cover multiple behaviors).
- `policies/candidates.json` is the only checked-in candidate matrix.
- `baselines/baseline-contract.json` records the gate and the three comparison
  descriptors. Those comparison policies are not executed by this gate.
- `run_context_eval.py` seeds a temporary SQLite database and text artifacts,
  runs the real repository and asynchronous assembler, persists snapshots, and
  sends the actual assembled messages to an offline capture provider. Attachment
  selections are persisted by the real runtime start/plan path. Compaction uses
  authored fake-provider JSON and checks the resulting persisted checkpoint.
  Source aliases translate fixture labels to generated database IDs; expected
  facts and ranges are never computed from assembler output.

From `services/agent-core`, with `src` on `PYTHONPATH`:

```powershell
$env:PYTHONPATH = Join-Path $PWD 'src'
python evals/context/run_context_eval.py
python evals/context/run_context_eval.py --verify-mutations
python -m pytest tests/test_context_eval_sentinels.py -q
```

`run_suite(*, mutation=None)` returns an `EvalReport` with per-case assertion
details, observed source/range/error/status evidence, and failures. CLI output is
JSON. Unknown arguments exit 2. Normal assertion failures or surviving mutations
exit 1; an unexpected infrastructure exception also exits nonzero.

`--verify-mutations` first runs the normal suite, then each scoped negative
control in `mutations.py`. The same expectations apply to every run:

| Mutation | Behavioral evidence that kills it |
| --- | --- |
| `drop_history` | Missing required historical fact and source |
| `inject_foreign_source` | Seeded sibling/foreign source or secret appears |
| `upgrade_external_trust` | External attachment gets policy trust/system role |
| `drop_selected_attachment` | Selected fact, source, and exact range disappear |
| `ignore_context_limit` | Oversized current input reaches provider instead of expected error |
| `leave_compaction_running` | Provider failure leaves persisted attempt running |

Mutations wrap real repository/packing/budget/cleanup boundaries and are restored
after each case. No assertion checks whether a mutation was enabled. The JSON
report includes killed/survived status and failed cases for each mutation. Run
the suite serially: scoped patches and the temporary data directory environment
are process-global. No production code, provider configuration, credentials, or
user database is modified.

Raw prompts, provider replies, and large reports belong in the repository-root
`.artifacts/context-evals/` directory and must not be committed. Release-only
Ollama/OpenAI-compatible probes are intentionally outside the daily CI suite.
