# Changelog

## 1.0.7 - 2026-10-27

This release brings the chat and privacy branch into the desktop release, replaces the setup-heavy workspace with a compact interface, and fixes repeated-run filename changes. The release version intentionally advances from 0.1.6 to 1.0.7.

### Added

- **Ask Sortly:** optional instructions before analysis and multi-message refinement afterward, using the active provider and fallback configuration.
- Structured move and exclude rules matched by topic, exact extension, or relative path glob. Topic matching is resolved against known non-sensitive file IDs; extension and path matching run locally.
- Question and clarification responses that leave the plan unchanged when no rules are returned. Conflicting newer instructions replace older decisions for the same files.
- Automatic shortening of filenames over 80 characters under the existing **Suggest cleaner file names** setting. Content-grounded concise names are preferred; otherwise existing wording is shortened at a word boundary. Extensions are retained, and collisions use the existing suffix handling.
- Explicit extension-change labels in file review and a selected-file count in the confirmation dialog. Extension changes rename files; they do not convert their contents.
- A recorded working demo GIF in the rewritten README, with clearer installation, development, testing, build, privacy, and limitation documentation.

### Interface changes

- Replaced the sidebar/settings clutter with Preview, Settings, Providers, and History pills.
- Moved the larger Sortly mark and name to the upper left, removed the redundant main heading, and placed Analyze beside the folder picker.
- Added the expanding Ask Sortly composer, aligned send control, typing feedback, and text reveal for actual model replies. Queued messages are identified as queued rather than presented as fabricated responses.
- Replaced the large analysis banner with a small monochrome wave below Ask Sortly. Analyze becomes Cancel while analysis is running. The Analyze action uses a moving grayscale gradient.
- Consolidated file review into compact rows. Folder destinations appear at group level; filenames are not repeated in boxed summaries. Renames, extension changes, edited destinations, exclusion state, and warnings remain visible. Editing and evidence stay in Details.
- Added stable accessible labels for review edit fields during validation errors.
- Moved Undo last move to History and kept Provider Studio in its own tab.
- Removed the Group similar files locally control and preserved the remaining backend settings.
- Added proportional glass-style analysis switches with green enabled states and larger setting labels. The existing overall theme is retained.
- Kept Preview accessible before analysis with an honest empty state. Proposed folder trees are no longer displayed before confirmation; the confirmed destination tree is shown only after Apply.
- Improved narrow-window layout and retained keyboard, reduced-motion, and forced-colors support for the new controls.

### Fixed

- Corrected the chat prompt to require exactly one selector per rule. Invalid JSON/schema responses receive one bounded repair attempt before provider fallback; rejected paths, unsupported actions, and privacy constraints remain validated. The working demo exposed this issue with a real model returning combined topic/extension selectors.
- Files already at their intended path no longer collide with themselves and acquire unnecessary `-1` suffixes on a second run.
- Collision resolution recognizes an existing source even when it is reached as a suffix candidate, preventing an existing `name-1` from advancing to `name-2` solely because of a rerun.
- The same no-op handling now covers manual file edits, group selection, and chat refinement. Normal real collisions still get unique names. Preview and chat responses populate `already_organized`.
- Keeping an existing long filename during a folder-only move no longer truncates it when rename suggestions are disabled.
- Selecting a different folder clears the previous conversation, draft, instructions, stale completion state, and Undo availability. Re-selecting the same folder or cancelling the picker preserves chat. Instructions entered before selecting the first folder survive that initial selection.
- Cancelled analysis preserves its abort outcome, stops the UI's waiting state, and discards late results instead of publishing a stale plan.
- Legitimate PGN chess records and unified patch/diff files are recognized as specialized text formats. They are not treated as TXT/INI merely because their contents are readable text.
- Generic text sniffing no longer proves that an unfamiliar extension is wrong. Unsupported model extension-mismatch claims are ignored unless local evidence confirms the mismatch.
- Routine clearer-name suggestions and excluded uncertain files no longer inflate the Needs a look count. Security concerns retain precedence.
- Tightened classification guidance so a missing matching folder does not justify using Misc or an unrelated existing category. Folder names remain generated from actual content and user instructions.
- Prevented PDF container bytes, numeric object IDs, and decimal constants from being mistaken for payment-card contents. Payment-card detection requires a valid checksum and payment-card context.
- Reduced broad finance false positives by requiring personal account details or multiple statement/payroll fields instead of ordinary discussion of payments, receipts, and balances.
- Isolated model-discovery tests from the user's saved provider configuration so the no-key test does not use a real stored credential.

### Packaging

- Updated root/frontend npm manifests and lockfiles, Tauri configuration, and the Rust package version to **1.0.7**.
- Rebuilt the Python sidecar and Windows x64 NSIS installer from the release source.
- Excluded optional scientific/embedding packages from the desktop bundle. No embedding runtime, local embedding download, or additional frontend dependency is introduced by this release.
- Preserved the existing AES PDF cryptography runtime and guarded Apply/Undo journal behavior.

### Known limitations

- The short-content confidence threshold remains conservative; short, meaningful files may still need review. Better category guidance does not guarantee model accuracy.
- Chat does not yet receive every confidence score, selection state, and scan-skip reason for diagnostic questions. Duplicate answers are hints rather than verified duplicate detection.
- Sensitive status leaves files unselected in their current location. It does not automatically create an identity-document category or a permission-protected folder.
- Normal organization can leave emptied source folders behind; automatic cleanup is not part of 1.0.7.
- OCR is not bundled, and the current pipeline does not provide visual image understanding or full audio/video content analysis.
- Cancelling a frontend analysis request may not stop a provider request already in progress.
- The installer is unsigned. Existing filename suffixes from earlier runs are not automatically removed.

### Verification

- 259 backend tests and 48 subtests passed, including reruns, collision handling, privacy, chat schema repair, and Apply/Undo safety.
- 21 frontend unit tests and all 10 browser workflow scenarios passed. Browser regression requests use isolated fixtures.
- Recorded a separate live-model demo: six sample files analyzed, DSA instructions applied, six moves confirmed, then Undo restored all six original content hashes.
- Verified the rebuilt Windows payload: bundled API startup, privacy exclusion, one schema-repair retry, long-name shortening, no-op reruns, and Undo. The installer was extracted without replacing an existing installation; its desktop app passed real Tauri IPC, API health, empty-folder analysis, and Settings checks.

## 0.1.6 - 2026-10-25

### Fixed

- Bundled the `cryptography` runtime required to read AES-encrypted PDFs.
- Included pypdf's cryptography providers in the packaged sidecar.
- Encrypted or otherwise undecryptable PDFs now fall back to bounded metadata/text handling instead of aborting the entire preview.
- Added regressions for AES-256 PDF extraction and missing PDF cryptography providers.

## 0.1.5 - 2026-10-25

### Fixed

- A model reply that is prose, malformed JSON, the wrong shape, truncated, or empty is now treated as a failed model instead of aborting the preview.
- Content analysis now falls back to the next configured model after an unusable reply.
- When every model produces an unusable reply, the error identifies each model and the parsing problem.
- Preview parsing failures now return a readable HTTP 502 message instead of the generic unexpected-error banner.
- Added regressions for planner fallback, all-models-failed reporting, and the preview API error response.

## 0.1.4 - 2026-10-25

### Fixed

- Removed `temperature` from default Anthropic requests because newer Claude models reject it with HTTP 400.
- Added compatibility retries for LiteLLM-backed classifier and fast-planner calls when a model or API rejects `temperature`.
- Preserved opt-in temperature settings for older models through `ORGANIZER_LLM_PARAMS`.

### Compatibility

- Improved model support across Anthropic and other API providers that reject an explicit `temperature` value.
- Added regressions covering default Anthropic request bodies, opt-in parameters, safe retry detection, and both LiteLLM call paths.

## 0.1.3 - 2026-10-25

Fixed Apply failures caused by Unicode filenames and legacy Windows console encodings.
