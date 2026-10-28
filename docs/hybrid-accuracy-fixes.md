# Hybrid accuracy and latency fixes

Implemented on `codex/hybrid-file-intelligence`, 28 October 2026.

## Problems and focused changes

- **Slow CPU embeddings:** the optional embedding path previously defaulted to Qwen
  0.6B. It now defaults to `all-minilm:22m`, with compact 480-character evidence
  cards for its short context. Qwen remains selectable through
  `ORGANIZER_EMBEDDING_MODEL=qwen3-embedding:0.6b`. Embeddings remain opt-in, local,
  digest-checked, privacy-filtered, and subject to the configurable file-count gate.
  No runtime or model download was added to the product.
- **Purpose confusion and sequential requests:** existing folder names from early
  batches could bias later batches without defining which document purposes fit.
  For 16 or more unresolved files, the same configured LLM now creates a bounded
  folder plan from the selected evidence. Each folder has a subject/purpose
  description and supporting card references. Two bounded classification batches
  can then run concurrently against this shared vocabulary. Names remain dynamic;
  there is no predefined category list or neighbor-based automatic assignment.
  Invalid folder plans produce a visible warning and preserve the sequential path.
  Local chat inference remains sequential. A transient provider failure gets one
  bounded retry; persistent errors still fail visibly.
- **Lost dates and versions:** a grounded name quote previously allowed the model
  to omit original identifiers. Ordinary and chat naming now protect original date
  and version tokens locally, shortening descriptive wording first. Unrepresentable
  constraints retain the original name or existing review behavior.
- **Broken relative references:** review exclusion could be overridden by group
  selection and explicit Apply edits. Initial review exclusions remain by default;
  an explicit group switch includes or excludes every member, with warnings
  preserved. Apply checks literal local references against all edited destinations,
  including unselected files; it rejects a broken reference before moving files.
  Valid relative layouts remain possible. Dependency bundles still use the existing
  partial-failure/Undo recovery; multi-file filesystem transactions are not atomic.
- **Repeated filesystem work during Apply:** each validation pass re-approved the
  same root twice and repeatedly resolved already-canonical paths and ancestors.
  Same-root approval is now shared within a pass, containment uses resolved paths,
  and ancestor traversal is bounded to the approved root. Alias paths retain a
  resolving fallback. No positive approval is cached across passes. Fresh checks
  around directory creation, content hashing and exclusive moves remain intact.
- **Missing diagnostics after a run:** new journal records retain scalar analysis
  status/timing counters and executor duration. Folder-planning evidence text is
  excluded. Older history records remain readable. The analysis cache version was
  advanced so previous classification decisions do not mask the revised planner.

## Verification

Relevant existing tests passed before changes: **146**.

Final checks:

- **531 Python tests and 53 subtests passed.** The existing FastAPI/httpx
  deprecation warning remains.
- **22 frontend tests passed; TypeScript and Vite production build passed.**
  Vite retains its bundle-size advisory. No CSS or visual design was changed.
- New regressions cover date/version preservation, compound extensions, arbitrary
  dataset-derived subjects, shared folder plans, bounded concurrent classification,
  invalid folder plans, transient provider retries, MiniLM input/query budgets,
  edited and unselected local dependencies, new project markers between validation
  passes, and parent symlinks. Existing stale-file, cross-root Undo, partial failure,
  privacy, chat, caching and extension-correction regressions also passed.

## Synthetic end-to-end pilot

The supplied Downloads folder was read only. A fresh disposable copy of the same
120 generated files was analyzed using local MiniLM and the configured
`deepseek/deepseek-flash` provider, with fresh analysis/vector caches. No answer-key
labels were supplied to either model. Equivalent folder labels were accepted when
checking the final subject and purpose against the key.

| Measure | Original observed result | Revised pilot |
|---|---:|---:|
| Unique readable documents correctly grouped | 89/96 | 96/96 |
| Readable documents plus exact copies correctly grouped | 100/108 | 108/108 |
| Original dates retained | 0/16 | 16/16 |
| Original `v2` tokens retained | 0/8 | 8/8 |
| Special cases kept for review | 0/12 moved outcome* | 12/12 kept |
| Analyze-to-preview time | user-reported approximately 177 s | 45.90 s |
| Local embedding stage | separate Qwen checks approximately 2 min | 4.55 s |
| Apply | separate 120-move check 18.51 s | 5.96 s for 108 accepted moves |
| Undo | separate 120-restore check 19.45 s | 3.65 s for 108 restores |

*The original journal cannot distinguish manual overrides from initial selections.
The revised pilot accepted the preview's default selections. Its 12 special cases
include four scan-only files without OCR, blank/nonsemantic inputs, ambiguous notes,
and the HTML/CSS pair.

Every original filename and SHA-256 hash was restored by Undo. Classification used
15 file batches, two workers, one folder-planning request, and 16 total provider
attempts. Folder planning took 4.54 s; classification wall time was 34.82 s. Every
nonduplicate still receives a validated per-file decision; this change does not
blindly propagate a neighbor's label.

These are single pilot observations, not controlled median speedups: the original
analysis breakdown was not retained, provider latency varies, and Apply accepted
different file counts. Earlier attempts exposed a stalled verbose test runtime and
a provider wait; they were retained as diagnostic reports and excluded from the
clean timing. Restarting the test-only Ollama runtime with stdout/stderr redirected
to files restored prompt embedding responses. The product does not launch that
runtime. A wider real-folder holdout and repeated cold/warm runs remain necessary.

Detailed generated reports are under `build/accuracy-review/revised/` (ignored):
`run.json`, `summary.json`, and the preserved stalled/provider-wait attempts.

## Files changed

- `app/core/folder_planning.py`: bounded dataset folder-plan schema and prompts.
- `app/core/content_intelligence.py`: shared folder planning, bounded concurrency,
  naming integration, reference metadata and stage counters.
- `app/core/naming.py`, `app/core/chat.py`: identifier-preserving naming.
- `app/core/semantic_index.py`, `.env.example`: MiniLM default and short-context budget.
- `app/core/safety.py`: less repeated resolution within fresh validation passes.
- `app/api.py`: reference validation after user edits and timing handoff.
- `app/core/executor.py`, `app/core/history.py`: durable diagnostic metadata.
- `app/core/content_cache.py`: cache version invalidation.
- `frontend/src/App.tsx`, `frontend/src/lib/review.ts`: retain review exclusions in
  bulk selection, using existing controls and styling.
- `tests/test_hybrid_accuracy_fixes.py`, `tests/test_semantic_index.py`,
  `frontend/tests/review.test.ts`: regression coverage and explicit Qwen compatibility.
- `scripts/benchmark-hybrid.py`, `docs/hybrid-file-intelligence.md`: default model and
  architecture documentation.

No release, push, or user-file reorganization was performed for this change.
