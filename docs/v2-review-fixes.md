# Sortly 2.0 review fixes

## Causes and changes

The 300-file synthetic review exposed gaps between content extraction, privacy
classification and chat rules. No supplied files were modified during investigation.

- **Privacy detection:** Aadhaar required grouping, passport numbers had no rule,
  and labelled card details required a valid checksum. Local rules now recognize
  labelled structured identifiers, passport-number formats and explicitly labelled
  card-length numbers. Numeric boundaries still reject decimals, scientific notation
  and unrelated reference numbers. This is bounded heuristic detection, not full DLP.
- **Private-key redaction:** only the PEM header was replaced. Redaction now removes
  the complete block before truncation, including flattened or missing-footer samples.
  Evidence cards redact defensively at the outbound boundary, using filename/metadata
  context. Sensitive evidence is excluded from the extraction cache; a one-time cache
  migration removes previously missed sensitive excerpts. This cannot retract earlier
  cloud requests or guarantee forensic removal from filesystem backups.
- **Extension mismatches:** Python and Markdown lacked strong text sniffing; CSV
  fallback did not recognize JSON. Bounded extraction now parses Python syntax without
  execution and recognizes structural CSS, JavaScript, YAML and Markdown signals.
  CSV fallbacks recognize other supported text formats. Rename guards still require
  strong local detection and grounded model evidence. Short or ambiguous code can
  remain unknown and require review.
- **Code grouping:** topic rules allowed CSS to merge with HTML and YAML with INI.
  Suggestions now carry locally verified language metadata. A versioned chat action
  groups matching code by that metadata under a dynamically chosen parent. Documents
  without code metadata retain their own rules. Sensitive files and dependent assets
  retain chat exclusions. Naming policies compose with grouping in the saved preview.
- **History:** the flat move list was capped at 100 entries. History now derives a
  folder tree from all journal entries, including cross-root destinations and legacy
  records. Every folder starts collapsed; opening it reveals children and filenames.
  Renaming/restoration indicators and per-run Undo/retry controls remain. This shows
  the recorded operation rather than claiming to scan current file locations.

## Changed files

- `app/core/privacy.py`, `evidence.py`, `content_cache.py`: detection, complete-block
  redaction, contextual redaction and versioned cache cleanup.
- `app/core/extractor.py`, `content_intelligence.py`: bounded format detection,
  defensive evidence cards and verified language metadata.
- `app/core/chat.py`, `chat_resolution.py`, `app/api.py`: structured language rules,
  complete-dataset context and naming-policy compatibility.
- `frontend/src/components/HistoryPanel.tsx`, `HistoryTree.tsx`, `ChatPanel.tsx`,
  `frontend/src/lib/history.ts`, `workspace.ts`: tree display and chat schema support.
- `tests/test_review_dataset_fixes.py`, `frontend/tests/history.test.ts`: privacy,
  redaction, cached analysis, format and rename guards, same/cross-root Apply/Undo,
  chat rule execution and complete history-tree counts.
- `scripts/collect-release.py`, `verify-release-artifacts.py`, release workflow,
  `tests/test_release_assets.py`: Windows installer ZIP, payload/hash/version checks
  and automatic native release builds on version tags.
- README, changelog, security support table and package/Tauri/Cargo version metadata
  describe 2.0.0. Earlier hybrid architecture changes are documented separately in
  [hybrid accuracy fixes](hybrid-accuracy-fixes.md).

## Validation

The pre-change baseline passed 152 relevant Python tests with 37 subtests, plus 24
frontend tests. Final local checks passed **580 Python tests and 53 subtests**, plus
**27 frontend tests**, the TypeScript check and Vite production build. The existing
FastAPI/httpx deprecation and Vite bundle-size advisories remain. Native CI repeats
the complete suite, frozen-service and desktop-startup checks on each platform.

On disposable copies of the supplied synthetic fixture:

| Check | Result |
| --- | --- |
| Planted sensitive files detected | 45/45, previously 28/45 |
| Ordinary files falsely flagged | 0/255 |
| Planted extension mismatches with correct strong local detection | 30/30 |
| Private-key bodies absent from outbound cards | 3/3 |
| CSS / HTML / YAML / INI verified independently | 10 / 23 / 8 / 6 |
| Files moved across separate roots | 255, zero failures |
| Files restored by Undo | 255, zero skips |
| Original names and SHA-256 hashes after Undo | all 300 restored/preserved |

The dataset validation uses fixture-provider decisions and makes **zero external
LLM calls**. It tests extraction, privacy, grounded extension guards, structured chat
rules, collision resolution and file integrity. It does not establish general LLM
semantic accuracy or a new end-to-end speed improvement. The first integration
attempt bypassed the API collision resolver and safely rejected colliding shortened
fixture names; the successful check uses that existing resolver.

Headless browser checks use mocked 300-file history records. They verify that every
folder starts collapsed, expanding a subfolder reveals files past the former 100-file
limit, other branches remain hidden, Undo/retry refreshes the record, and no Apply
request or page error occurs. Existing review-switch regressions remain in the suite.

Release packaging checks validate that the Windows ZIP contains exactly the built
versioned setup executable, every platform checksum matches, no unexpected asset is
published, and all application version files agree. Native startup and frozen-service
checks run on Windows x64, Linux x64 and Apple Silicon macOS before publication.
