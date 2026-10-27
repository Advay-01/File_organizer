# Changelog

## 1.0.10 - 2026-10-27

### File safety and recovery

- Fixed ordinary folders being rejected because an unrelated ancestor contained a standalone project manifest. Selected software projects, confirmed repository/build trees, protected directories, and embedded projects remain guarded.
- Fixed Undo for moves between separate source and destination trees. New journals record both approved roots, and restoration validates them in reverse. Destination conflicts and partial failures are reported and can be retried without overwriting files.
- Bound preview operations to the original file identity and full SHA-256 content fingerprint. Apply verifies each file again immediately before moving it and skips changed, replaced, or missing sources with a reanalysis message.
- Preserved fingerprints through cached analysis, chat refinement, destination edits, and renaming. The normal analysis flow reuses the scanned fingerprint; changed extraction results cannot be cached under the old digest.
- Rechecked paths around directory creation and final hashing, strengthened source-parent symlink/junction checks, and verified the copy fallback before removing the source.
- Made skipped operations visible in both CLIs and the legacy GUI, including failed runs that moved no files. Existing layout, styling, AI prompts, dynamic folder generation, and privacy gates are unchanged.
- Preserved older same-root history records. Legacy cross-root journals without approved-root metadata remain restricted because their original authorization cannot be inferred safely.

### Verification and packages

- Added 43 temporary-file regression cases covering project detection, cross-root undo, conflict retries, stale content, replaced files, cache reuse, path redirects, and CLI/GUI failure reporting.
- Local Windows validation: 320 backend tests and 53 subtests passed; all 21 frontend tests passed. Native release CI repeats tests and frozen-service/desktop startup checks on Windows x64, Linux x64, and Apple Silicon macOS before publication.
- The native package formats remain the Windows NSIS installer, Linux `.deb` and AppImage, and macOS Apple Silicon DMG, with per-platform SHA-256 manifests.
- Detailed changes, the complete file list, and remaining filesystem/journal limitations are documented in [the safety investigation](docs/safety-fixes.md).

## 1.0.9 - 2026-10-27

### Desktop platforms

- Added native Linux x64 `.deb` and AppImage packaging, plus an Apple Silicon macOS DMG. The Windows x64 NSIS installer remains available. Intel Mac installers are not included.
- Added a native GitHub Actions build matrix with isolated backend/frontend tests, frozen service regressions, desktop startup checks, and per-platform SHA-256 manifests. Platform packages are built on their native OS and architecture.
- Removed the Windows-only `.exe` assumption when launching the bundled service. Open folder now selects Explorer, macOS Finder's `open`, or Linux `xdg-open`, without invoking a shell.
- Added macOS Keychain and Linux Secret Service storage for the encryption key protecting provider credentials. Windows DPAPI is retained. A locked or unavailable secure store fails closed; there is no plaintext fallback. Environment-based provider keys and Ollama remain supported.
- Added OS-neutral provider and appearance wording and platform build instructions. Classification, scanning, and privacy detection are unchanged.
- Fixed safety validation of equivalent system path aliases, including macOS `/var` versus `/private/var` and Windows short versus long paths. Nested symlinks and junctions remain rejected.
- Preserved no-overwrite behavior on POSIX using exclusive native rename operations and an exclusive-copy fallback for unsupported filesystems. Windows keeps its existing non-replacing rename behavior; the existing Apply/Undo journal remains in use.

### Interface fixes

- The analysis indicator spans the Ask Sortly window and shows a thin light with a bright leading edge travelling straight from left to right. Reduced-motion mode keeps it static.
- Folder inclusion switches stay aligned on the right. On narrow screens warning badges sit under the folder label instead of pushing the switch onto a separate line. Alignment is checked from 320px through desktop widths.

### Distribution notes

- macOS packages use ad-hoc signatures and are not Apple notarized. Windows installers remain unsigned. The macOS build targets 13 or newer; Linux packages use Ubuntu 22.04 as their build baseline.
- Desktop API credentials, sample files, and CI test keychains are not bundled. New keyring dependencies are limited to non-Windows platforms; no embedding or scientific runtime is added.

### Verification

- Windows: 277 backend tests and 53 subtests passed. Linux and Apple Silicon: 273 backend tests and 53 subtests passed, with four Windows-specific tests skipped on each.
- All three targets passed 21 frontend tests and three Rust startup tests. The updated review and analysis indicator also passed all 10 local browser scenarios.
- Every native frozen service passed secure credential storage, blank subcategories, privacy exclusion, long-name shortening, chat schema repair, confirmed moves, no-op reruns, and Undo with all sample hashes restored. Only local fixture providers and generated files were used.
- Verified desktop startup and bundled API health on Windows, the installed Linux `.deb`, Linux AppImage, and the Apple Silicon `.app`. The Mac bundle also passed strict recursive signature verification.

## 1.0.8 - 2026-10-27

### Fixed

- An explicitly empty or whitespace-only model subcategory now uses the planner's existing `General` fallback instead of rejecting the entire batch. This fixes DeepSeek replies containing `subcategory: ""`, including the reported eighth file in a batch.
- Missing subcategories, null values, invalid types, invalid confidence values, and unknown or duplicate file IDs remain rejected. This does not bypass review, privacy checks, or confirmation.
- The classification prompt explicitly requests a non-empty subcategory, and the analysis cache version advances so previous cached decisions are refreshed.

### Packaging

- Rebuilt the Windows installer as **1.0.8** after the 1.0.7 release had already published. The complete UI, chat, privacy and rerun improvements are documented in the 1.0.7 entry below.
- Updated version metadata, the README download link, and supported-version documentation. The recorded 1.0.7 working demo remains applicable.

### Verification

- 261 backend tests and 53 subtests passed, including an eight-file batch with a blank subcategory on its eighth file, whitespace-only values, and strict rejection of invalid decision fields.
- The frozen API also passed the blank-subcategory regression, sensitive-file exclusion, long-name shortening, chat schema repair, two confirmed moves, a no-op rerun, and Undo with every original content hash restored. This check used a local fixture provider and generated sample files.
- Extracted the final installer and verified that its API executable matches the tested build. The desktop reports version 1.0.8 and passed real Tauri IPC, API startup, empty-folder analysis, and Settings checks without replacing the existing installation.

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
