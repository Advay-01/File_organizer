# Evaluating the hybrid planner

The benchmark compares the existing planner with deterministic duplicate reuse
and optional local semantic candidates. It never calls the executor or moves
files. Generated fixtures, application state, analysis caches, and embedding
caches live in temporary directories. A reviewed manifest reads only its listed
files, after the normal protected-root and symlink checks.

## Start with the deterministic pipeline check

From the repository root on Windows:

```powershell
.\.venv\Scripts\python.exe scripts/benchmark-hybrid.py --generated-files 100 --output build/hybrid-simulation.json
```

On another platform, use the Python interpreter in the project's activated
environment. An existing report is never overwritten; choose a new output name.

The default **simulation** uses a marker-reading completion stub and a small
hashed-token vector stub. Neither is a real language model or trained embedding
model. The generated corpus has eight topic/purpose groups and a known set of
exact duplicate documents. Perfect fixture agreement only demonstrates that the
pipeline and metrics can operate together. It does not establish classification
accuracy, embedding quality, or a production speedup.

`--min-files 0` explicitly exercises semantic routing for a tiny fixture. The
application default remains 75 unresolved eligible files. Below the configured
threshold, the backend is not constructed, probed, loaded, or queried.

The report includes these independently cached variants:

1. `baseline`: duplicate reuse disabled and semantic candidates disabled.
2. `deterministic-reuse`: conservative exact-duplicate reuse enabled, semantic
   candidates disabled.
3. `hybrid-1`: duplicate reuse plus semantic candidates; additional embedding
   model arguments add `hybrid-2`, `hybrid-3`, and so on.

Each variant has an empty-application-cache pass followed by a warm cache pass.
No variant receives another variant's analysis or vector cache. The baseline
still benefits from the application's existing evidence and decision cache on its
warm pass; caching is not a benefit exclusive to embeddings.

The first implementation uses semantic candidates to coordinate evidence and
batch ordering. It does not automatically propagate an embedding neighbor's
classification. Hybrid mode can therefore have the same model workload as
deterministic reuse while adding embedding and prompt overhead. The benchmark
reports that outcome rather than assuming it is faster.

## Build a reviewed evaluation set

Collect five to ten representative folders, with permission to inspect their
contents. Use dedicated copies of nonsensitive material when possible. Keep
development data and a separate holdout set. Avoid putting near-duplicate document
families in both sets, because that can make retrieval look better than it is.

For every file, review its desired relationship group and optionally record
acceptable folder names and filenames. Group IDs describe human judgments about
which files belong together; they are independent of the literal folder label.
For example, two acceptable labels might be `Physics/Lecture Notes` and
`Education/Physics Notes`.

Create a JSON manifest next to its dataset directory:

```json
{
  "schema_version": 1,
  "dataset_name": "coursework-holdout",
  "split": "holdout",
  "root": "reviewed-coursework",
  "instructions": [
    "Group by course, keeping exercises separate from lecture notes."
  ],
  "known_folders": [],
  "files": [
    {
      "path": "scan001.pdf",
      "group": "physics-notes",
      "accepted_targets": ["Physics/Lecture Notes", "Education/Physics Notes"],
      "accepted_names": ["Heat_Transfer_Lecture.pdf"],
      "review_expected": false
    },
    {
      "path": "incoming/scan002.pdf",
      "group": "physics-exercises",
      "accepted_targets": ["Physics/Exercises"],
      "accepted_names": ["Entropy_Practice.pdf"],
      "review_expected": false
    },
    {
      "path": "unclear.txt",
      "group": "unresolved-document",
      "review_expected": true
    }
  ]
}
```

`root` is relative to the manifest, or an explicit absolute directory. Every
`path` must be relative to that root; parent traversal, duplicate paths, source
symlinks, and software-project paths are rejected. Only listed files are read.
All label fields except `path` are optional. Missing labels produce `null` quality
metrics rather than an invented perfect score. The small example above documents
the format; it is not a supplied reviewed dataset.

Keep the evaluation data outside the Sortly source checkout, for example under
`C:\Sortly-Evaluation\`. The normal software-project protections intentionally
reject attempts to organize data inside a detected repository.

Repeat the benchmark for each reviewed folder. Keep its report separate, then
compare both per-folder results and aggregate counts. A weighted average can hide
failure on one particular file format or task, so retain the breakdowns.

## Run actual model comparisons explicitly

Real provider requests require both `--real-provider` and one explicit
`--llm-model`. Configure that provider's environment credentials and endpoint as
usual. The harness does not activate credentials from the desktop's saved provider
configuration. A cloud LLM receives the same policy-filtered evidence as normal
planning; sensitive documents remain excluded by default. Use an installed local
LLM if the evaluation must remain entirely local.

```powershell
.\.venv\Scripts\python.exe scripts/benchmark-hybrid.py --manifest "C:\Sortly-Evaluation\coursework.json" --real-provider --llm-model "ollama/your-installed-chat-model" --embedding-model "qwen3-embedding:0.6b" --allow-rename --output build/coursework-real-models.json
```

Replace the example chat-model name with an installed model. Embeddings use the
loopback Ollama endpoint, an already-installed model, and its verified model
digest. The harness does not download models or fall back to a cloud embedding
service. A missing service or model produces an explicit unavailable embedding
status and ordinary planner fallback. That run is **not evidence about the
requested embedding model**; check `semantic_backend_used` before comparing it.

Repeat `--embedding-model` to compare two or three installed local candidates
under the same dataset, rules, LLM, and cache setup. These arguments only exercise
the model matrix in simulation mode; the same lexical stub is used there, so they
do not measure differences between the named models.

Compare models with a common conservative evidence budget first, for example
`--embedding-max-chars 1000`. The same explicit character budget is passed to
every model and recorded in the report. A lightweight model's token context can
be much smaller than Qwen's; the default 12,000-character card is not appropriate
for every model. Characters are not tokens, so even a conservative character
budget is not a guaranteed context fit for every language and tokenizer. The
client sets `truncate: false`; an oversized input produces a visible unavailable
backend/fallback result instead of silently embedding different truncated text.
Run long-context models with larger budgets as a separate experiment so the
effects of model choice and extra evidence are not confused.

Use `--allow-rename` for a separate naming evaluation. Renaming changes the amount
of work that can safely be reused across duplicates. First compare grouping-only
runs, then naming-enabled runs, while keeping that setting consistent across the
variants being compared.

## Interpret the measurements

| Measurement | Definition and caveat |
| --- | --- |
| False merge rate | Wrong same-folder file pairs divided by all predicted same-folder pairs |
| False split rate | Missed human-labelled same-group pairs divided by all human-labelled same-group pairs |
| Grouping precision / recall | Pairwise grouping agreement, independent of literal category names |
| Assignment coverage | Selected proposals divided by all files; prevents abstention from hiding difficult cases |
| Review rate | Files unselected, lacking sufficient evidence, or rated low/insufficient by the planner |
| Approved target accuracy | Exact case-insensitive agreement with a supplied acceptable destination label |
| Approved name accuracy | Exact case-insensitive agreement with a supplied acceptable filename |
| Unapproved rename rate | Changed filenames outside their supplied acceptable names; only labelled renames count |
| Model workload | `llm_file_evaluations`, `llm_batches`, provider attempts, and `llm_prompt_chars` |
| Planner timing | Extraction, LLM, embedding provider setup/probe, embedding inference/model load, cache, and total |
| Wall timing | Manifest-file path validation, hashing, and planning; this is not desktop UI latency |

Undefined ratios are `null`. Unselected files do not create predicted same-folder
pairs, but their missing expected relationships still count as splits. An
unapproved filename is a disagreement with the reviewed labels; this metric does
not independently prove whether an alternate name is factually unsupported.

Reports contain aggregate results by default. `--include-predictions` includes
per-file IDs and proposed names and targets, which may be private derived data.
It does not include full document evidence. Keep reports under the same privacy
policy as the evaluation files.

Application-cache cold is not equivalent to model-cold or disk-cold. The harness
does not unload an existing model, reset OS caches, clear a provider's caches, or
download missing weights. Ollama's reported `embedding_model_load_seconds` is
recorded when an actual local model was used; zero may indicate an already-warm
runtime. Model download time is unmeasured because downloads are never performed.

Run multiple repetitions, vary dataset order, and report median and slower-tail
latencies on named hardware. Include model versions/digests, file counts and
formats, privacy exclusions, token/request usage, failed runs, and backend fallback
status. A handful of reviewed folders is an initial diagnostic, not proof of
general accuracy. Genuine user acceptance and correction-propagation quality
require separate human review and interaction testing.
