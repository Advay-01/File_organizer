# Hybrid file intelligence: development branch

This branch implements a conservative first version of the attached design. It
keeps the existing content-intelligence planner, editable preview, safe executor,
source fingerprints, history, and undo. Folder and subfolder labels still come
from the selected evidence and user instructions. There is no predefined topic
taxonomy and no automatic file movement through chat.

## Routing and scope

1. Scan and bind each source to its original SHA-256 fingerprint and file identity.
2. Reuse content-derived extraction, then check semantic evidence quality and privacy.
3. Build an ephemeral graph of exact duplicates, declared document derivations,
   possible versions, and selected local asset dependencies.
4. Reuse a strong duplicate classification only when content and parent context
   match, renaming is off, and explicit instructions do not distinguish the copies.
5. If enabled and at least 75 unresolved representative files remain, build local
   semantic vectors and suggest neighbours. Otherwise use direct evidence cards.
6. Order related candidates together and supply bounded, batch-local relationship
   hints to the existing LLM planner. Every nonduplicate file still receives its own
   validated subject/purpose decision and per-file naming evidence.
7. Let chat refine a saved preview through structured, versioned rules. Apply remains
   explicit and goes through the existing path, fingerprint, and journal checks.

The 75-file threshold is configurable and applies after reusable decisions and
duplicates are removed. A mostly cached large folder can therefore skip the
embedding backend entirely. The default keeps semantic embeddings disabled until
an installed local model is deliberately configured. Missing/invalid local models
produce a visible analysis warning and fall back to ordinary evidence planning.

This version intentionally uses simple nearest-neighbour candidates. UMAP,
HDBSCAN, a graph database, cross-encoders, and a second taxonomy-planning model
are not required. Candidate lookups have a bounded work budget, and similarity
scores are not displayed as calibrated confidence percentages.

## What is optimized, and what remains experimental

Exact duplicate reuse can reduce repeated file evaluations. Unchanged files also
reuse the existing extraction/decision cache, with user instructions and parent
context included in decision invalidation.
Instruction-dependent decisions also include the selected dataset's fingerprint
and identity context. If an accompanying proposal changes its project name, the
unchanged status report is reconsidered rather than retaining an old destination.

Embedding hints currently help coordinate batches and provide evidence of possible
relationships. They do **not** automatically inherit a neighbour's destination or
skip per-file validation. The hybrid path can consequently have the same LLM file
workload as deterministic reuse while adding embedding and prompt overhead. Real
accuracy and end-to-end speed must be measured before considering stronger routing
or enabling it by default. The existing baseline already uses batched LLM requests;
it is not a one-request-per-file strawman.

## Local semantic runtime

The desktop bundle gains no required ML/scientific dependency. Embeddings use an
already-running local Ollama service and an already-installed embedding model.
Neither Ollama nor model weights are downloaded automatically.

Example development configuration in `.env`:

```dotenv
ORGANIZER_SEMANTIC_ENABLED=1
ORGANIZER_EMBEDDING_BASE_URL=http://127.0.0.1:11434
ORGANIZER_EMBEDDING_MODEL=qwen3-embedding:0.6b
ORGANIZER_EMBEDDING_MIN_FILES=75
ORGANIZER_EMBEDDING_MAX_CHARS=12000
```

Only loopback endpoints are permitted. Redirects and HTTP proxies are disabled;
cloud model entries are rejected. Installed model digests are checked so retagged
models invalidate vector caches. Cache keys distinguish evidence, extractor/input
version, model identity, and document/query tasks. Filename/folder label edits do
not recompute content-derived vectors. Sensitive or weak evidence is excluded,
and potentially sensitive user-query vectors are not persisted.

Candidates to compare on reviewed folders include
[all-minilm](https://ollama.com/library/all-minilm),
[qwen3-embedding:0.6b](https://ollama.com/library/qwen3-embedding), and
[bge-m3](https://ollama.com/library/bge-m3). These are experiment candidates, not
a verified ranking for Sortly. Their download sizes and context limits differ
substantially. Use a shared conservative input budget, such as 1000 characters,
for an initial comparison; characters are not tokens. Oversized requests fail
visibly because the client asks the service not to silently truncate inputs.

## Extraction quality and dependent assets

Image dimensions, format names, creator software, and generic document titles do
not establish semantic content. Meaningful title-only evidence is weak. Insufficient
evidence leaves a file unselected and marked for review, even if a model claims high
confidence. Strong content can still reach the normal dynamic topic planner.

Optional OCR uses already-installed Tesseract, plus Poppler for PDFs. PDF rendering
is limited to two pages at a bounded resolution, with renderer/OCR timeouts and a
temporary workspace. Images are resized before OCR. Missing tools, blank results,
or failures produce a review warning rather than a guessed subject or filename.
OCR tools are not bundled or installed by this change.

XMP IDs are untrusted assertions and can be copied or stale; conflicting IDs do
not force grouping. Matching filename patterns, authors, or timestamps alone do
not establish a relationship. Resource references are resolved only against the
already-selected files; referenced paths and URLs are never opened or fetched.

Local resource dependencies and corroborated companion files remain unselected
for relationship review. HTML with unselected local resources also requires review.
The current executor is not a bundle relocation engine; automatic chat rules do
not override that review requirement. Manual preview acceptance remains explicit.

## Chat state and naming

Initial queued instructions are included in the first planning request as explicit
user intent and in decision-cache keys. The existing chat refinement step then
records validated version-1 rules on the saved plan. Rule revisions increase only
when a rule-producing message creates a new preview; clarification prevents Apply
until a subsequent rule resolves it.

Selectors have exactly one scope: `topic`, `extension`, `glob`, or proposed `folder`.
Extension/glob/folder membership is deterministic. A folder selector includes its
proposed subfolders, allowing a folder merge with no second topic-classification
call. Topic rules evaluate every eligible file in bounded batches. Semantic search
can change processing order but never truncates an instruction's global scope.
Complete topic matches are cached against the whole relevant file context, topic,
model list, and resolver version. Context changes invalidate them.

For large datasets, intent parsing receives bounded, explicitly labelled samples;
these samples are never the final membership scope. Unknown file IDs are rejected.

Naming policies support enable/disable, generic-only names, a 20–120-character
length limit, and date preservation. They preserve extensions, snapshots, privacy
exclusions, and independent folder/exclusion decisions. An explicit request to
enable names can generate missing per-file proposals from cached evidence, after
checking that the source still matches the preview. Semantic indexing is disabled
for that naming-only pass. Every proposed name still passes the existing evidence
and filename validators; chat text itself is never used as an arbitrary filename.
An explicit rename of a previously organized no-op becomes a selected operation
when its evidence supports it; explicit exclusions remain excluded. Collision
suffixes reserve space inside the active length budget and preserve complete
extensions/dates. Impossible policies leave a visible review warning rather than
silently violating a constraint. Disabling renaming takes precedence over an older
length preference. A newly typed per-file filename is an explicit user override.

## Privacy and evaluation

Settings now describe that a selected cloud provider receives redacted filenames,
folder context, extracted passages, and metadata. Sensitive files stay excluded
unless redacted cloud analysis is explicitly enabled; sensitive files remain
excluded from automatic chat operations and the semantic cache. Local embeddings
do not make a later cloud-planning request local. Chat exclusions after analysis
must not be mistaken for a pre-analysis privacy boundary.

See [the evaluation guide](hybrid-evaluation.md) for the generated fixture check,
reviewed manifest format, baseline/reuse/hybrid comparison, explicit real-provider
model matrix, and cold/warm measurement caveats. Collect five to ten permissioned,
manually reviewed folders with a separate holdout set. No real-folder accuracy or
real-model speed claim is established by the deterministic fixture benchmark.

## Verification on this branch

- Before implementation: 320 backend tests and 53 subtests passed.
- After implementation: 517 backend tests and 53 subtests passed.
- All 21 frontend tests and the frontend production build passed.
- Tests use disposable files, fixture models, and local fixture HTTP servers.
- A generated 100-file simulation recorded 100 baseline file evaluations versus
  80 with deterministic reuse and 80 with hybrid candidates. All application-warm
  passes used zero model evaluations. Hybrid input was 135102 prompt characters,
  versus 127995 for deterministic reuse: optional semantic hints added overhead.
- These fixture workload results do not establish real-model accuracy or a
  latency improvement. No real embedding model was downloaded or benchmarked.
