# Changelog

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
