# Source review — 2026-11-02

Scope: architecture and publication readiness; focused reading of update verification, credential storage, project file authorization, terminal execution, Markdown URL policy, release tooling and CI. This is not a complete security audit.

## Findings addressed in this source release

1. **High — update trust and distribution identity.** Production update feeds, installer URL allowlists and a signing public key were tied to another application identity. This project uses its own distribution URLs and an empty production public key; update checks stop before network access until independently configured.
2. **High — local identity isolation.** Reusing application IDs, credential namespaces, environment variables and database names could mix installations. The application ID is `io.github.zcchou.agent`; packages, modules, credentials, paths and environment variables consistently use the new identity. Existing user data is not imported.
3. **Medium — terminal capability documentation.** Prior development guidance understated the executable capability. The README and security guidance state that terminal commands execute as the OS user, with no filesystem/network sandbox implied by project cwd validation.
4. **Medium — source publication hygiene.** Internal development reports, historical plans, release notes, local databases, logs, node_modules, build outputs and private credentials are excluded. A root README and MIT license establish installation and reuse terms.
5. **Medium — release pipeline assumptions.** Binary publication must use the public source repository and verify its own successful workflow provenance. Independent signing setup and installer acceptance remain prerequisites for an official binary release.
6. **Dependency advisories.** The initial npm audit reported 1 critical, 2 high, 2 moderate and 5 low findings. Compatible updates now lock DOMPurify 3.4.17, Vite 8.3.5, Vitest 5.0.5 and source-map-js 1.2.2. The final audit reports 0 critical/high/moderate and 5 low findings. These are package advisories, not proof that every advisory is exploitable in this application.

## Remaining findings

- **Low — transitive KaTeX advisories.** Five npm audit entries remain through Mermaid and the math-rendering dependency graph. npm's automatic resolution proposes breaking major-version changes. Keep this separate from the identity change and validate math/diagram rendering when upgrading.
- **Medium — intermittent draft regression test.** The full UI run passed 168/169; editing a historical question into a continuation once cleared the next draft in `chat-composer-flow.spec.mjs:257`. The same scenario passed in the 15/15 targeted rerun after dependency updates. The involved application logic was unchanged. The cause is unconfirmed; do not treat a passing retry as proof that a race is fixed.
- **Low — initial frontend bundle size.** The production entry chunk is approximately 1.1 MB before gzip and Vite reports large chunks. Consider splitting optional rich rendering features when startup performance becomes a priority.

## Validation evidence

- Frontend: 32 test files, 116 unit tests passed after dependency updates; production build passed.
- UI: initial full run 168 passed/1 failed; post-update targeted chat and Markdown run 15 passed, including the previously failed scenario.
- Rust: native Host test binary 241 passed/2 ignored; formatting and strict Clippy passed. The restricted environment cannot exercise native Windows file-handle and loopback behavior correctly, so the native test binary was run with those permissions available.
- Sidecar: rebuilt and verified Core 0.14.2, IPC 16, schema 23, build `schema23-skills-v1` through the actual compatibility handshake.
- Release helpers: 24 tests passed, plus a focused check that a published release targets the verified source commit.
- Context evaluator and negative mutations: standalone evaluation passed. See source validation notes for the Python suite result.
- Publication hygiene: no old product-name matches in staged source, no inherited Git history/remotes, and no staged local databases, private keys, logs or executable sidecars. Pattern scanning is not a guarantee that arbitrary sensitive text cannot exist.

## Retained controls and limitations

- File tools validate paths through Host authorization, including Windows aliases and link escapes. This does not constrain arbitrary shell code.
- Update feeds and packages require signatures and immutable allowed release URLs once configured. Test signing fixtures are synthetic and are not production keys.
- Markdown URL policy limits clickable protocols and remote images; native desktop permissions and provider/network behavior still need ongoing review.
- CI includes Python, frontend and Windows Host checks. Repository branch protection is a separate GitHub setting.
- The runtime and protocol version remain unchanged to avoid an unrelated compatibility migration. A full installer build, real startup/upgrade acceptance and live-model tests are separate from source readiness.
