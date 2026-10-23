# Fast mode (10,000 files in minutes)

## Run it
```
python -m app.fast_run C:\Downloads                      # dry run + summary
python -m app.fast_run C:\Downloads --apply              # move files
python -m app.fast_run C:\Downloads -m deepseek/deepseek-flash -m gemini/gemini-3.8-flash
```
Undo: `python -m app.main C:\Downloads --undo`

## Use it from the API / GUI
Replace the call to `build_plan(...)` with:
```python
from app.core.fast_planner import plan_files_fast, config_from_env

res = plan_files_fast(
    files, output_root, models, known_folders,
    config=config_from_env(),
    progress=lambda stage, done, total: job.update(stage=stage, done=done, total=total),
)
moves = res.moves     # list of Move(src, dest, folder, reason)
res.stats             # files, units, calls, rate_limited, unclassified_units, seconds, taxonomy
```
`progress` stages: `reading files`, `designing folders`, `classifying` -> drive a real progress bar.
Safe to call from a sync FastAPI endpoint or a background thread.

## What makes it fast
| Old | Fast mode |
|---|---|
| 20 files per call, one call at a time | ~100 files per call, many calls at once |
| Model writes path + folder + new name + reason per file (~35 tokens) | Model writes `[id, folder_number]` (~9 tokens) |
| Every file sent separately | `IMG_0001.jpg ... IMG_1500.jpg` style content-less files are sent once and share the answer |
| Folder names invented per batch (drifts) | One taxonomy call first, so all batches use the same folders |
| One provider | All listed models pull from one queue; a rate-limited model just does less |
| 429 / bad JSON = crash or fallback | adaptive back-off; bad JSON splits the batch; dead models are dropped |

## Automatic profiles (chosen by how many files you have)
| Files | Profile | What it does |
|---|---|---|
| up to 12 | `tiny` | one request sent to your top 2 models at once, 600 characters per file |
| 13 to 300 (a typical run) | `balanced` | folders designed from **all** files first, then small parallel batches of 25 with 600 characters per file |
| over 300 | `fast` | folders designed from a sample, batches of 100, 250 characters per file |

`balanced` is the accuracy-first middle: the model sees more of each file, fewer files per request, and one
shared folder list so names stay consistent. It costs roughly one extra short request versus the fastest setup.
Force one with `ORGANIZER_PROFILE=tiny|balanced|fast`. Anything you set yourself
(`ORGANIZER_BATCH_SIZE`, `ORGANIZER_SNIPPET_CHARS`, `--batch-size`) overrides the profile.

## Tuning (`.env`)
```
ORGANIZER_MODELS=deepseek/deepseek-flash,gemini/gemini-3.8-flash   # first = preferred
ORGANIZER_BATCH_SIZE=100          # bigger = fewer calls (Gemini free tier), but slower per call
ORGANIZER_SNIPPET_CHARS=250       # more text = better accuracy, more tokens
ORGANIZER_GEMINI_RPM=10           # set to YOUR limit from aistudio.google.com -> Rate Limit
ORGANIZER_DEEPSEEK_CONCURRENCY=48 # DeepSeek docs allow up to 2500 concurrent
ORGANIZER_LLM_PARAMS={"reasoning_effort":"low"}   # JSON passed to the model; see below
```
**Thinking is now handled for you** (the likely reason even 4 files were slow):
- DeepSeek: thinking is ON at high effort by default per its docs; fast mode sends `thinking: disabled`.
- Gemini 3 models cannot fully disable thinking; fast mode sends `reasoning_effort: low`.
- Override per provider in `.env`, e.g.
  `ORGANIZER_LLM_PARAMS={"deepseek":{"thinking":{"type":"enabled"}},"gemini":{"reasoning_effort":null}}`
  (`null` removes a key and restores that provider's own default).

**Small folders (<= batch size) use ONE request, sent to your top 2 models at the same time;
the first valid answer wins.** Put two models in `ORGANIZER_MODELS` for the lowest latency.

**Measure on your machine:** `python -m app.diag` prints cold/warm timings per model, and the
same request with default thinking so you can see exactly what it costs. Paste it if still slow.

Known providers (deepseek, gemini, openai, groq, openrouter, ollama) are called directly over HTTPS
with the standard library: no `litellm` import (often seconds on Windows). Other providers still
work through litellm, imported only when used. Needs the `DEEPSEEK_API_KEY` / `GEMINI_API_KEY` etc.
in `.env`. `ORGANIZER_<PROVIDER>_BASE_URL` overrides an endpoint.

## Writing fast code (the rules this follows)
1. **Measure first** (`app.diag`, `stats` in the result). Never guess where time goes.
2. **Latency is output tokens + thinking + round trips.** Ask for the least text, turn thinking down, make fewer calls.
3. **Do independent work at the same time** (batches, models), **never the same work twice** (dedupe, cache).
4. **Pay import and startup costs once or never** (lazy heavy imports, no per-request clients where avoidable).
5. **Fail fast and visibly**: dead key = immediate clear error, not retries; rate limit = back off, not crash.
6. **Keep the model out of the safety path**: ids in, ids out; paths and names are validated by code.
7. **Structure**: scanner -> extractor -> fast_planner (+ llm_http) -> executor. Each stage has one job and is testable with a fake LLM.

## Time estimate you can trust
`LLM calls ~ files / batch_size + 2`. Then:
- **No RPM cap (DeepSeek):** seconds to ~1 minute for 10,000 files.
- **RPM-capped (Gemini free):** minutes ~ calls / RPM. E.g. 10,000 files = ~90 calls, so 10 RPM ~ 9 min,
  30 RPM ~ 3 min. Raise `ORGANIZER_BATCH_SIZE` to 200 to halve this. Listing DeepSeek too removes the wall.

## Honest limits
- Accuracy depends on the snippet. 250 characters is usually enough for a topic, not always.
- Files with no readable text (photos, scans) are grouped by name pattern, not content.
- Test with a small folder first and read the dry-run summary before `--apply`.

