# Sortly safety fixes — 2026-10-27

## Findings and changes

### Project detection

`assert_safe_root()` previously treated any project marker in any ancestor as proof
that the selected directory belonged to a software project. A standalone
`package.json`, `pyproject.toml`, or `Cargo.toml` in a higher-level directory could
therefore block ordinary document folders.

The selected directory still rejects every supported project marker. Ancestor
directories require stronger evidence: `.git`, or a manifest accompanied by a
corresponding lock file or `src` tree. Protected directory names also protect their
descendants. Recursive scans skip embedded Git projects, and move validation checks
for embedded project directories on both sides of a move.

### Undo across separate roots

The executor accepted a separate source root but did not journal either approved
boundary. Undo then validated both the moved file and its restoration destination
against the output root, rejecting valid restorations to another tree.

New runs record canonical `source_root` and `output_root` values. Undo validates the
journal's output root against the requested history folder and reverses the original
boundaries: it reads from the original output root and restores into the original
source root. Move paths cannot broaden these boundaries. Invalid or incomplete root
metadata is rejected. Missing original directories can be recreated safely.

Restoration keeps exclusive destination creation, reports occupied original paths
and locked files, persists each restored flag, and supports retrying only the
remaining moves. Interrupted move-completion records remain recoverable. Legacy
journals without root metadata remain restricted to same-root restoration; missing
legacy completion flags correctly mean a completed move.

### Stale previews

Preview operations previously retained paths and classification decisions without
binding them to the file's original bytes or identity. Path validation alone could
not detect a changed or replaced file at the same pathname.

Each scanned file now receives a complete SHA-256 digest and an identity snapshot
(device, file ID/inode, size, modification time, and creation/change time). The
snapshot follows the file through cached analysis, suggestions, moves, user edits,
and chat copies. Unscanned planner inputs are bound before model calls. Source
snapshots are local and excluded from serialized file-analysis inputs and cached
classification decisions.

Apply compares identity and hashes the entire source again immediately before the
move, after durable journaling. Missing, modified, replaced, or nonregular sources
are skipped individually with a reanalysis message. Other valid selected operations
can proceed, and API responses report partial outcomes. Both CLIs return a failure
status and print skipped-file reasons; the legacy GUI displays incomplete execution
results, including runs that moved zero files.

Path validation runs before creating destination directories, after directory
creation, and after fingerprint verification. It checks source parents as well as
destination parents for links/junctions. Source paths and approved roots retain their
lexical spelling during validation so canonicalization cannot hide a substituted
link. Hashing checks the opened descriptor and pathname identity before and after
reading. The copy fallback also verifies copied bytes and checks for source changes
before removal, preserving the source and cleaning up its output on failure.

The normal content-intelligence flow hashes once during scanning and once during
Apply; preview edits and model analysis reuse the original digest. Changed extraction
results cannot be cached under an earlier digest.

## Preserved behavior

There are no UI layout, styling, prompt, taxonomy, or embeddings changes. Local
evidence analysis, AI classification, dataset-dependent folders/subfolders,
renaming, preview selection, chat refinement, caches, privacy gates, operation
history, and undo remain in the existing architecture.

## Files changed

| File | Purpose |
| --- | --- |
| `app/core/safety.py` | Project evidence, file snapshots, final move checks, copy verification, path validation |
| `app/core/history.py` | Recorded undo boundaries, legacy completion compatibility, restoration validation |
| `app/core/executor.py` | Journal roots/digests, stale-operation reporting, final verified execution |
| `app/core/scanner.py` | Bind scanned files to their original content and identity |
| `app/models/schemas.py` | Local-only source snapshot on `FileInfo` |
| `app/core/planner.py` | Snapshot-bearing moves and pre-analysis binding |
| `app/core/fast_planner.py` | Preserve snapshots through fast planning |
| `app/core/cached_planner.py` | Preserve snapshots through cache hits and destination adjustments |
| `app/core/content_intelligence.py` | Reuse scanned digests, propagate snapshots, reject changed evidence for caching |
| `app/core/evidence.py` | Use the shared guarded SHA-256 reader |
| `app/api.py` | Preserve snapshots through edits; report individual stale operations |
| `app/main.py` | Report failures and return a nonzero Apply exit status |
| `app/fast_run.py` | Report failures even when per-file executor output is suppressed |
| `app/gui.py` | Report incomplete or empty failed execution through existing dialogs |
| `tests/test_root_boundaries.py` | Project detection and same-/cross-root undo regressions |
| `tests/test_preview_identity.py` | Identity, content, cache, copy, path-race, CLI and GUI regressions |
| `tests/test_safety.py` | Update unreadable-file injection for the shared snapshot reader |
| `tests/test_api.py` | Assert individual missing-file reporting and continued valid moves |
| `tests/test_apply_safety.py` | Updated move hooks and partial stale-preview expectations |
| `docs/safety-fixes.md` | This investigation and verification report |

## Verification

- Before modifications: **73 relevant existing tests passed**.
- Added **43 regression cases** across the two new test modules.
- Final complete Python suite: **320 passed, 53 subtests passed**.
- Frontend suite: **21 passed**.
- `git diff --check`: passed.
- One existing FastAPI/Starlette TestClient deprecation warning remains.

Tests use temporary source/destination trees, stubbed models, and injected failures.
The final full run also isolated the default application state and cache in a
temporary directory. Separate-device behavior was simulated with `EXDEV`; no real
user files were moved or deleted.

## Limits and remaining security considerations

- Legacy cross-root journals without approved-root metadata cannot safely infer
  authorization from arbitrary recorded file paths, so those restores remain
  restricted. New cross-root runs carry the required boundaries.
- Journals are local JSON, without cryptographic authentication. Boundary checks
  reject paths outside recorded roots, but cannot establish authenticity if an
  attacker rewrites the roots and the move records together. Journal access must
  remain trusted.
- Final checks reduce the preview-to-execution race window, but portable path-based
  rename/copy operations do not provide an atomic filesystem transaction against
  hostile concurrent pathname changes between the last check and the OS call.
  Applications should not concurrently modify the selected trees during Apply.
- Ancestor project recognition is deliberately conservative and evidence-based;
  it is not a complete software-project classifier.
- Verification ran on Windows. Other-platform native move branches were not run on
  their respective operating systems in this session.
