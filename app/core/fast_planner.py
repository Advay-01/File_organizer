"""Fast bulk planner: built for thousands of files (target: 10,000 in minutes).

Why it is fast (the old planner made one 20-file call at a time, each returning
verbose JSON with a reason + new name for every file):

1. Parallel local extraction (thread pool) instead of one file at a time.
2. Near-duplicate collapse: content-less files with the same name pattern
   (IMG_0001.jpg ... IMG_1500.jpg) are classified once and the answer is shared.
3. One small "taxonomy" call sees a sample of the files and fixes the folder list.
4. Every other call returns ONLY `[id, folder_number]` pairs (~8 tokens per file
   instead of ~35). Output tokens are what LLM latency is made of.
5. Big batches (default 100 files) sent CONCURRENTLY; a shared work queue lets
   every configured model pull jobs, so a fast model (DeepSeek, 2500 concurrent
   requests allowed) naturally does most of the work while a slow/rate-limited
   one (Gemini free tier) still contributes.
6. 429s are handled with adaptive pacing, not crashes. Bad JSON -> batch is split.
   Dead models (bad key / retired model name) are dropped; the run continues.

Nothing here can write outside the output folder; ids (not paths) cross the LLM
boundary, so the model cannot invent a path.
"""
from __future__ import annotations

import asyncio
import json
import os
import random
import re
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable, Dict, List, Optional, Set

from app.core import llm_http
from app.core.classifier import extract_json, temperature_rejected
from app.core.extractor import extract_snippet
from app.core.planner import Move, ensure_source_snapshots
from app.models.schemas import FileInfo

AsyncCompletionFn = Callable[[str, List[dict]], Awaitable[str]]
ProgressFn = Callable[[str, int, int], None]

# --------------------------------------------------------------------------- config


@dataclass
class ModelLimits:
    concurrency: int = 8   # parallel in-flight requests for this model
    rpm: int = 0           # requests per minute; 0 = no fixed cap (429s still adapt)


_PROVIDER_DEFAULTS = {
    "deepseek": ModelLimits(48),     # docs: 2500 concurrent allowed, no fixed RPM
    "openai": ModelLimits(24),
    "anthropic": ModelLimits(12),
    "openrouter": ModelLimits(16),
    "gemini": ModelLimits(6),        # free tier is RPM-limited: set ORGANIZER_GEMINI_RPM
    "groq": ModelLimits(6),
    "ollama": ModelLimits(2),        # local CPU/GPU: more parallelism does not help
}


@dataclass
class FastConfig:
    # None = chosen automatically from the number of files (see resolve_profile).
    # Anything you set explicitly (code, CLI or .env) always wins over the profile.
    batch_size: Optional[int] = None
    snippet_chars: Optional[int] = None
    taxonomy_sample: Optional[int] = None
    taxonomy_chars: Optional[int] = None
    rename: bool = False             # smart renaming (opt-in: costs ~10 extra output tokens per file)
    single_call_max: int = 12        # at most this many files -> one hedged request, no taxonomy
    max_folders: int = 30
    min_folder_files: Optional[int] = None   # None = profile: 1 for tiny runs, 2 otherwise
    extract_workers: int = 16
    request_timeout: float = 90.0
    max_attempts: int = 10
    group_threshold: int = 4        # collapse content-less name patterns bigger than this
    backoff_floor: float = 0.5
    backoff_cap: float = 30.0
    llm_params: dict = field(default_factory=dict)
    limits: Dict[str, ModelLimits] = field(default_factory=dict)


def config_from_env() -> FastConfig:
    """ORGANIZER_BATCH_SIZE, ORGANIZER_SNIPPET_CHARS, ORGANIZER_LLM_PARAMS (JSON, e.g.
    reasoning/thinking off), and per provider ORGANIZER_<PROVIDER>_CONCURRENCY / _RPM."""
    cfg = FastConfig()
    if os.getenv("ORGANIZER_BATCH_SIZE"):
        cfg.batch_size = int(os.environ["ORGANIZER_BATCH_SIZE"])
    if os.getenv("ORGANIZER_SNIPPET_CHARS"):
        cfg.snippet_chars = int(os.environ["ORGANIZER_SNIPPET_CHARS"])
    if os.getenv("ORGANIZER_RENAME", "").strip().lower() in ("1", "true", "yes", "on"):
        cfg.rename = True
    raw = os.getenv("ORGANIZER_LLM_PARAMS", "").strip()
    if raw:
        cfg.llm_params = json.loads(raw)
    return cfg


# Size-based profiles. A typical run is ~100 files, where accuracy matters more than
# shaving seconds, so the middle profile spends a little more time and tokens:
#   tiny      (<= single_call_max): one hedged request, 600 chars/file
#   balanced  (<= 300 files): folders designed from ALL files, then small parallel batches
#             of 25 with 600 chars/file (the model sees less per request, so it reads it better)
#   fast      (> 300 files): big batches, 250 chars/file, folders designed from a sample
_PROFILES = {
    # tiny: no folder list is designed first, so EVERY folder is model-named; folding one-file
    # folders into Misc would erase them all, hence min_folder_files=1 there.
    "tiny":     dict(batch_size=25,  snippet_chars=600, taxonomy_sample=400, taxonomy_chars=200, min_folder_files=1),
    "balanced": dict(batch_size=25,  snippet_chars=600, taxonomy_sample=400, taxonomy_chars=200, min_folder_files=2),
    "fast":     dict(batch_size=100, snippet_chars=250, taxonomy_sample=200, taxonomy_chars=120, min_folder_files=2),
}


def resolve_profile(cfg: FastConfig, n_files: int) -> FastConfig:
    """Fill every unset (None) option from the profile chosen by file count."""
    from dataclasses import replace

    name = os.getenv("ORGANIZER_PROFILE", "").strip().lower()
    if name not in _PROFILES:
        name = "tiny" if n_files <= cfg.single_call_max else "balanced" if n_files <= 300 else "fast"
    chosen = {k: v for k, v in _PROFILES[name].items() if getattr(cfg, k) is None}
    out = replace(cfg, **chosen)
    out.profile = name  # type: ignore[attr-defined]
    return out


def limits_for(model: str, cfg: FastConfig) -> ModelLimits:
    if model in cfg.limits:
        return cfg.limits[model]
    prov = model.split("/")[0].lower()
    base = _PROVIDER_DEFAULTS.get(prov, ModelLimits(8))
    key = prov.upper().replace("-", "_")
    return ModelLimits(
        int(os.getenv(f"ORGANIZER_{key}_CONCURRENCY", base.concurrency)),
        int(os.getenv(f"ORGANIZER_{key}_RPM", base.rpm)),
    )


# --------------------------------------------------------------------------- helpers

_BAD = re.compile(r'[<>:"\\|?*\x00-\x1f]')
_DIGITS = re.compile(r"\d+")


def clean_folder(name) -> str:
    """'A/B/C' -> 'A/B' (max two levels); strips anything unsafe in a path part."""
    parts = [_BAD.sub("", p).strip(". ") for p in str(name).replace("\\", "/").split("/")]
    parts = [p[:40] for p in parts if p and p not in (".", "..")][:2]
    return "/".join(parts) or "Misc"


def _signature(f: FileInfo):
    return (f.extension, _DIGITS.sub("#", Path(f.filename).stem.lower()))


def _error_kind(e: Exception) -> str:
    code = getattr(e, "status_code", None)
    name = type(e).__name__
    if code == 429 or "RateLimit" in name:
        return "rate"
    if code in (400, 401, 403, 404, 422) or name in ("AuthenticationError", "NotFoundError",
                                                    "PermissionDeniedError", "BadRequestError"):
        return "fatal"  # retrying the same request cannot fix these
    if isinstance(e, ValueError):
        return "bad_output"
    return "transient"  # timeouts, 5xx, connection errors


def _run(coro):
    """Run a coroutine from sync code, even if a loop is already running (FastAPI)."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    with ThreadPoolExecutor(1) as ex:
        return ex.submit(asyncio.run, coro).result()


# --------------------------------------------------------------------------- extraction


# Formats that need real CPU to parse (pure-Python parsers hold the GIL, so threads
# do not help; separate processes do). Everything else is read in a plain loop,
# which measured ~3x FASTER than a thread pool for small text files.
HEAVY_EXTS = {".pdf", ".docx", ".pptx", ".xlsx", ".xlsm", ".epub", ".zip", ".rar", ".tar",
              ".gz", ".tgz", ".heic"}


def _safe_snippet(args) -> str:
    path, max_chars = args
    try:
        return extract_snippet(path, max_chars)
    except Exception:
        return ""


def extract_parallel(files: List[FileInfo], max_chars: int, workers: int = 0,
                     progress: Optional[ProgressFn] = None, min_heavy_for_pool: int = 40) -> None:
    """Attach snippets. Light files: simple loop. Heavy files: process pool when worthwhile."""
    heavy = [f for f in files if f.extension in HEAVY_EXTS]
    light = [f for f in files if f.extension not in HEAVY_EXTS]
    done = 0
    total = len(files)

    for f in light:
        f.snippet = _safe_snippet((f.path, max_chars))
        done += 1
        if progress and done % 500 == 0:
            progress("reading files", done, total)

    if heavy:
        n_workers = workers or min(8, os.cpu_count() or 2)
        results = None
        if len(heavy) >= min_heavy_for_pool and n_workers > 1:
            try:
                from concurrent.futures import ProcessPoolExecutor
                with ProcessPoolExecutor(max_workers=n_workers) as ex:
                    results = list(ex.map(_safe_snippet, [(f.path, max_chars) for f in heavy],
                                          chunksize=8))
            except Exception:
                results = None  # e.g. frozen app / restricted env: fall back to serial
        if results is None:
            results = [_safe_snippet((f.path, max_chars)) for f in heavy]
        for f, s in zip(heavy, results):
            f.snippet = s
    if progress:
        progress("reading files", total, total)


# --------------------------------------------------------------------------- units


@dataclass
class Unit:
    uid: int
    rep: int                # index into files of the file the model actually sees
    members: List[int]      # every file index that inherits this unit's answer
    label: str


def build_units(files: List[FileInfo], group_threshold: int) -> List[Unit]:
    units: List[Unit] = []
    groups: Dict[tuple, List[int]] = defaultdict(list)
    for i, f in enumerate(files):
        if f.snippet:
            units.append(Unit(len(units), i, [i], f.filename))
        else:
            groups[_signature(f)].append(i)
    for idxs in groups.values():
        if len(idxs) >= group_threshold:
            rep = idxs[0]
            label = f"{files[rep].filename} (+{len(idxs) - 1} similar files)"
            units.append(Unit(len(units), rep, idxs, label))
        else:
            for i in idxs:
                units.append(Unit(len(units), i, [i], files[i].filename))
    return units


def _line(u: Unit, files: List[FileInfo], snippet_chars: int) -> str:
    s = files[u.rep].snippet[:snippet_chars].replace("|", " ").replace("\n", " ")
    return f"{u.uid}|{u.label.replace('|', ' ')}|{s}"


# --------------------------------------------------------------------------- prompts

TAXONOMY_SYSTEM = (
    "You design a folder structure for a user's files from a sample (one per line: name|snippet).\n"
    "Return ONLY JSON: {\"folders\": [\"Category\", \"Category/Subcategory\", ...]}.\n"
    "Rules: at most {MAX} folders; broad and useful (e.g. \"AI/Machine Learning\", \"DSA\", "
    "\"Finance/Invoices\", \"Photos\"); at most 2 levels; no one-off folders; cover the whole sample."
)


class _Pick(str):
    """A folder answer that can also carry the model's suggested file name (`.name`)."""

    name: str = ""


def clean_filename(name, original: str) -> str:
    """Make a model-suggested name safe: keep the ORIGINAL extension, strip path/unsafe
    characters, spaces -> underscores, cap length. Falls back to the original name."""
    ext = Path(original).suffix
    stem = str(name).strip()
    if ext and stem.lower().endswith(ext.lower()):
        stem = stem[: -len(ext)]
    stem = re.sub(r"[^\w\s\-.()]", "", stem.replace("/", " ").replace("\\", " "))  # letters(any language), digits
    stem = re.sub(r"\s+", "_", stem).strip("._- ")[:80]
    if len(stem) < 2 or not re.search(r"[A-Za-z0-9]", stem):
        return original
    if stem.upper() in {"CON", "PRN", "AUX", "NUL"} or re.fullmatch(r"(COM|LPT)\d", stem.upper()):
        stem += "_"
    return stem + ext


def _classify_system(folders: List[str], rename: bool = False) -> str:
    if folders:
        listing = "\n".join(f"{i}. {f}" for i, f in enumerate(folders, 1))
        rule = ("Pick the best folder NUMBER. Only if nothing fits, give a short new folder name "
                "as a string (max 2 levels, e.g. \"Travel/Tickets\"). Use 0 only if the file is "
                "truly unclassifiable.")
    else:
        listing = "(none yet)"
        rule = ("Give each file a short, broad folder name as a string (max 2 levels, e.g. "
                "\"AI/Notes\"). Reuse the same name for similar files. Use \"Misc\" if unclear.")
    if rename:
        fmt = ("{\"r\": [[id, folder, \"name\"], ...]} covering every id once. The third item is a clean "
               "descriptive file name WITHOUT extension (2-6 words, e.g. \"Binary Tree Notes\") ONLY when "
               "the current name is unclear (Untitled, scan001, final_v2, IMG_...) and the snippet makes "
               "the content obvious; otherwise \"\". Never invent facts that are not in the snippet.")
    else:
        fmt = "{\"r\": [[id, folder], ...]} covering every id once."
    return (
        "You sort files into folders by CONTENT (use the snippet; fall back to the file name).\n"
        f"Folders:\n{listing}\n\n{rule}\n"
        "Input: one file per line as `id|name|snippet`.\n"
        f"Respond with ONLY JSON, no extra text: {fmt}"
    )


def _resolve(val, folders: List[str]) -> str:
    if isinstance(val, bool) or val is None:
        return "Misc"
    if isinstance(val, (int, float)) or (isinstance(val, str) and val.strip().isdigit()):
        n = int(val)
        return folders[n - 1] if 1 <= n <= len(folders) else "Misc"
    return clean_folder(val)


def _parse_rows(raw: str, expected: Set[int], folders: List[str], rename: bool = False) -> Dict[int, str]:
    data = extract_json(raw)
    rows = data
    if isinstance(data, dict):
        rows = data.get("r") or data.get("results") or data.get("actions")
    if not isinstance(rows, list):
        raise ValueError("response has no result list")
    out: Dict[int, str] = {}
    for row in rows:
        newname = ""
        if isinstance(row, dict):
            uid, val, newname = row.get("id"), row.get("folder"), row.get("name") or ""
        elif isinstance(row, (list, tuple)) and len(row) >= 2:
            uid, val = row[0], row[1]
            if rename and len(row) >= 3 and isinstance(row[2], str):
                newname = row[2]
        else:
            continue
        try:
            uid = int(uid)
        except (TypeError, ValueError):
            continue
        if uid in expected and uid not in out:
            pick = _Pick(_resolve(val, folders))
            pick.name = newname if rename and isinstance(newname, str) else ""
            out[uid] = pick
    if not out:
        raise ValueError("no usable rows in response")
    return out


# --------------------------------------------------------------------------- LLM plumbing


async def _litellm_call(model: str, messages: List[dict], params: dict) -> str:
    import litellm

    litellm.drop_params = True  # ignore params a provider does not support
    kwargs = dict(model=model, messages=messages, response_format={"type": "json_object"}, **params)
    try:
        resp = await litellm.acompletion(temperature=0, **kwargs)
    except Exception as exc:  # noqa: BLE001 - retry once without temperature, otherwise re-raise
        if not temperature_rejected(exc):
            raise
        resp = await litellm.acompletion(**kwargs)
    return resp.choices[0].message.content or ""


class _ModelState:
    """Pacing + health for one model. Created inside the running event loop."""

    def __init__(self, name: str, limits: ModelLimits, cfg: FastConfig):
        self.name, self.limits, self.cfg = name, limits, cfg
        self.base_interval = 60.0 / limits.rpm if limits.rpm else 0.0
        self.interval = self.base_interval
        self.next_slot = 0.0
        self.pause_until = 0.0
        self.alive = True
        self.consec_fail = 0
        self.lock = asyncio.Lock()

    async def pace(self) -> None:
        async with self.lock:
            now = time.monotonic()
            start = max(now, self.next_slot, self.pause_until)
            self.next_slot = start + self.interval
        if start > now:
            await asyncio.sleep(start - now)

    def rate_limited(self) -> None:
        now = time.monotonic()
        if now < self.pause_until:  # one 429 burst = one back-off step
            return
        self.interval = min(max(self.interval * 2, self.cfg.backoff_floor), self.cfg.backoff_cap)
        self.pause_until = now + self.interval * 2

    def success(self) -> None:
        self.consec_fail = 0
        if self.interval > self.base_interval:
            self.interval = max(self.base_interval, self.interval * 0.8)

    def hiccup(self) -> None:
        self.consec_fail += 1
        if self.consec_fail >= 8:
            self.alive = False


@dataclass
class _Job:
    ids: List[int]
    attempts: int = 0
    tried: Set[str] = field(default_factory=set)


async def _race(models: List[str], messages: List[dict], call: AsyncCompletionFn,
                cfg: FastConfig, parse, stats: dict, hedge: int = 2):
    """Hedged request for ONE-OFF calls (small folders, taxonomy): send the same request to
    the top `hedge` models at once and take the first valid answer. Latency becomes the
    minimum of the models instead of the first model's (possibly slow/failing) latency."""

    async def one(m: str):
        errs: List[str] = []
        for attempt in range(2):
            stats["calls"] += 1
            try:
                return parse(await asyncio.wait_for(call(m, messages), cfg.request_timeout))
            except asyncio.CancelledError:
                raise
            except Exception as e:
                kind = _error_kind(e)
                errs.append(f"{m}: {kind}: {str(e)[:200]}")
                if kind == "fatal":
                    break
                await asyncio.sleep(min(cfg.backoff_floor * (2 ** attempt) * (2 if kind == "rate" else 1),
                                        cfg.backoff_cap))
        raise RuntimeError("; ".join(errs))

    tasks = [asyncio.create_task(one(m)) for m in models[:hedge]]
    errs: List[str] = []
    pending = set(tasks)
    try:
        while pending:
            done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
            for t in done:
                if t.exception() is None:
                    return t.result()
                errs.append(str(t.exception()))
        raise RuntimeError(" | ".join(errs))
    finally:
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def _taxonomy(units: List[Unit], files: List[FileInfo], models: List[str],
                    call: AsyncCompletionFn, cfg: FastConfig, stats: dict) -> List[str]:
    n = cfg.taxonomy_sample
    tchars = cfg.taxonomy_chars or 120
    if len(units) <= n:
        sample = units
    else:  # half stratified by extension (rare types get seen), half uniform random
        rnd = random.Random(0)
        by_ext: Dict[str, List[Unit]] = defaultdict(list)
        for u in units:
            by_ext[files[u.rep].extension].append(u)
        for v in by_ext.values():
            rnd.shuffle(v)
        sample, chosen = [], set()
        while len(sample) < n // 2 and any(by_ext.values()):
            for v in by_ext.values():
                if v and len(sample) < n // 2:
                    u = v.pop()
                    sample.append(u)
                    chosen.add(u.uid)
        rest = [u for u in units if u.uid not in chosen]
        sample += rnd.sample(rest, min(n - len(sample), len(rest)))

    messages = [
        {"role": "system", "content": TAXONOMY_SYSTEM.replace("{MAX}", str(cfg.max_folders))},
        {"role": "user", "content": "\n".join(
            f"{files[u.rep].filename}|{files[u.rep].snippet[:tchars].replace('|', ' ')}" for u in sample)},
    ]

    def parse(raw: str) -> List[str]:
        data = extract_json(raw)
        lst = data.get("folders") if isinstance(data, dict) else data
        if not isinstance(lst, list) or not lst:
            raise ValueError("no folders")
        out: List[str] = []
        for x in lst:
            c = clean_folder(x)
            if c != "Misc" and c.lower() not in {o.lower() for o in out}:
                out.append(c)
        return out[: cfg.max_folders]

    try:
        return await _race(models, messages, call, cfg, parse, stats)
    except RuntimeError:
        return []  # not fatal: the classify stage can name folders itself


async def _classify_units(units: List[Unit], files: List[FileInfo], folders: List[str],
                          models: List[str], call: AsyncCompletionFn, cfg: FastConfig,
                          progress: Optional[ProgressFn], stats: dict) -> Dict[int, str]:
    states = {m: _ModelState(m, limits_for(m, cfg), cfg) for m in models}
    queue: asyncio.Queue = asyncio.Queue()
    result: Dict[int, str] = {}
    failed: Set[int] = set()
    errors: List[str] = []
    unit_by_id = {u.uid: u for u in units}
    system = _classify_system(folders, cfg.rename)
    total = len(units)
    fatal = {"hit": False}

    for i in range(0, len(units), cfg.batch_size):
        queue.put_nowait(_Job([u.uid for u in units[i : i + cfg.batch_size]]))

    def alive() -> List[_ModelState]:
        return [s for s in states.values() if s.alive]

    def report() -> None:
        if progress:
            progress("classifying", len(result), total)

    def give_up(job: _Job, why: str) -> None:
        for uid in job.ids:
            if uid not in result:
                result[uid] = "Misc"
                failed.add(uid)
        errors.append(why)
        report()

    def abort_everything(job: _Job) -> None:
        fatal["hit"] = True
        give_up(job, "no working model left")
        while not queue.empty():
            give_up(queue.get_nowait(), "no working model left")
            queue.task_done()

    async def handle(state: _ModelState, job: _Job) -> None:
        if not state.alive:
            queue.put_nowait(job)
            return
        if job.attempts >= cfg.max_attempts:
            give_up(job, f"gave up on {len(job.ids)} item(s) after {job.attempts} attempts")
            return
        live = {s.name for s in alive()}
        if live <= job.tried:
            job.tried.clear()
        if state.name in job.tried:  # let a different model take it
            queue.put_nowait(job)
            await asyncio.sleep(0.05)
            return

        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": "\n".join(_line(unit_by_id[i], files, cfg.snippet_chars)
                                                   for i in job.ids)},
        ]
        stats["calls"] += 1
        try:
            raw = await asyncio.wait_for(call(state.name, messages), cfg.request_timeout)
            rows = _parse_rows(raw, set(job.ids), folders, cfg.rename)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            kind = _error_kind(e)
            errors.append(f"{state.name}: {kind}: {str(e)[:160]}")
            job.attempts += 1
            if kind == "rate":
                stats["rate_limited"] += 1
                state.rate_limited()
                queue.put_nowait(job)
            elif kind == "fatal":
                state.alive = False
                job.tried.add(state.name)
                if not alive():
                    abort_everything(job)
                else:
                    queue.put_nowait(job)
            elif kind == "bad_output":
                if len(job.ids) > 1 and job.attempts >= 2:
                    h = len(job.ids) // 2
                    queue.put_nowait(_Job(job.ids[:h]))
                    queue.put_nowait(_Job(job.ids[h:]))
                else:
                    queue.put_nowait(job)
            else:
                state.hiccup()
                job.tried.add(state.name)
                if not alive():
                    abort_everything(job)
                else:
                    queue.put_nowait(job)
                    await asyncio.sleep(min(cfg.backoff_floor * (2 ** job.attempts), cfg.backoff_cap))
            return

        state.success()
        result.update(rows)
        missing = [i for i in job.ids if i not in rows]
        if missing:
            queue.put_nowait(_Job(missing, attempts=job.attempts + 1))
        report()

    async def worker(state: _ModelState) -> None:
        while state.alive:
            await state.pace()          # wait for permission BEFORE taking a job
            job = await queue.get()
            try:
                await handle(state, job)
            except asyncio.CancelledError:
                raise
            except Exception as e:      # never let a bug hang the run
                give_up(job, f"internal error: {e!r}")
            finally:
                queue.task_done()

    tasks = [asyncio.create_task(worker(s)) for s in states.values()
             for _ in range(s.limits.concurrency)]
    await queue.join()
    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)

    stats["unclassified_units"] = len(failed)
    if fatal["hit"]:
        raise RuntimeError("All models failed:\n  " + "\n  ".join(dict.fromkeys(errors[-8:])))
    return result


# --------------------------------------------------------------------------- public API


@dataclass
class FastResult:
    moves: List[Move]
    stats: dict


def _unique_dest(dest: Path, taken: set) -> Path:
    if str(dest) not in taken and not dest.exists():
        return dest
    i = 1
    while True:
        cand = dest.with_name(f"{dest.stem}_{i}{dest.suffix}")
        if str(cand) not in taken and not cand.exists():
            return cand
        i += 1


def plan_files_fast(
    files: List[FileInfo],
    output_root: str,
    models: List[str],
    known_folders: Optional[List[str]] = None,
    config: Optional[FastConfig] = None,
    progress: Optional[ProgressFn] = None,
    acompletion_fn: Optional[AsyncCompletionFn] = None,
    extract: bool = True,
) -> FastResult:
    """Plan moves for many files quickly. `acompletion_fn(model, messages) -> str` is
    injectable for tests; by default litellm.acompletion is used."""
    if not models:
        raise ValueError("At least one model is required")
    ensure_source_snapshots(files)
    cfg = resolve_profile(config or config_from_env(), len(files))
    t0 = time.monotonic()
    # every key the CLI/API reads must exist on EVERY path (tiny runs never enter the batch engine)
    stats = {"files": len(files), "calls": 0, "rate_limited": 0, "profile": cfg.profile,
             "units": 0, "unclassified_units": 0, "renamed": 0}

    if extract:
        todo = [f for f in files if not f.snippet]
        extract_parallel(todo, cfg.snippet_chars, cfg.extract_workers, progress)
    stats["read_seconds"] = round(time.monotonic() - t0, 2)

    units = build_units(files, cfg.group_threshold)
    stats["units"] = len(units)

    async def default_call(m: str, msgs: List[dict]) -> str:
        if llm_http.is_supported(m):   # direct HTTP: no litellm import, thinking off by default
            return await llm_http.direct_call(m, msgs, cfg.llm_params, cfg.request_timeout)
        return await _litellm_call(m, msgs, cfg.llm_params)

    call = acompletion_fn or default_call

    async def go():
        folders: List[str] = []
        for k in known_folders or []:
            c = clean_folder(k)
            if c.lower() not in {x.lower() for x in folders}:
                folders.append(c)

        result: Dict[int, str] = {}
        if len(units) <= cfg.single_call_max:
            # Tiny run: ONE hedged request, no taxonomy step.
            ids = [u.uid for u in units]
            msgs = [{"role": "system", "content": _classify_system(folders, cfg.rename)},
                    {"role": "user", "content": "\n".join(_line(u, files, cfg.snippet_chars) for u in units)}]
            if progress:
                progress("classifying", 0, len(units))
            try:
                result = await _race(models, msgs, call, cfg,
                                     lambda raw: _parse_rows(raw, set(ids), folders, cfg.rename), stats)
            except RuntimeError as e:
                stats["race_error"] = str(e)[:300]   # fall through to the resilient engine
            remaining = [u for u in units if u.uid not in result]
            if remaining:
                result.update(await _classify_units(remaining, files, folders, models, call, cfg,
                                                    progress, stats))
            if progress:
                progress("classifying", len(units), len(units))
        else:
            if progress:
                progress("designing folders", 0, 1)
            t1 = time.monotonic()
            for t in await _taxonomy(units, files, models, call, cfg, stats):
                if t.lower() not in {x.lower() for x in folders}:
                    folders.append(t)
            stats["taxonomy_seconds"] = round(time.monotonic() - t1, 2)
            if progress:
                progress("designing folders", 1, 1)
            result = await _classify_units(units, files, folders, models, call, cfg, progress, stats)
        stats["taxonomy"] = list(folders)
        return result, folders

    t_llm = time.monotonic()
    result, folders = _run(go())
    stats["llm_seconds"] = round(time.monotonic() - t_llm, 2)

    # ---- canonical folder names (case-insensitive), then expand units to files
    canon: Dict[str, str] = {f.lower(): f for f in folders}
    folder_of: Dict[int, str] = {}
    unclassified: Set[int] = set()
    for u in units:
        raw = result.get(u.uid, "Misc")
        name = canon.setdefault(raw.lower(), raw)
        for m in u.members:
            folder_of[m] = name
            if raw == "Misc":
                unclassified.add(m)

    # ---- fold tiny model-invented folders back into their parent (or Misc)
    protected = {f.lower() for f in folders} | {"misc"}
    counts = Counter(folder_of.values())
    for idx, name in list(folder_of.items()):
        if name.lower() not in protected and counts[name] < cfg.min_folder_files:
            parent = name.split("/")[0]
            folder_of[idx] = parent if (parent.lower() in protected or counts.get(parent, 0) >= cfg.min_folder_files) else "Misc"

    # ---- optional smart renaming: only files the model actually READ (has a snippet) and that
    # are not part of a collapsed look-alike group (one name cannot fit a whole group)
    new_name: Dict[int, str] = {}
    if cfg.rename:
        for u in units:
            if len(u.members) == 1 and files[u.rep].snippet:
                suggestion = getattr(result.get(u.uid), "name", "")
                if suggestion:
                    cleaned = clean_filename(suggestion, files[u.rep].filename)
                    if cleaned != files[u.rep].filename:
                        new_name[u.rep] = cleaned
    stats["renamed"] = len(new_name)

    root = Path(output_root)
    taken: set = set()
    moves: List[Move] = []
    for idx, f in enumerate(files):
        folder = folder_of.get(idx, "Misc")
        fname = new_name.get(idx, f.filename)
        dest = _unique_dest(root.joinpath(*folder.split("/"), fname), taken)
        taken.add(str(dest))
        reason = "unclassified" if idx in unclassified else "content match"
        if idx in new_name:
            reason += " + renamed"
        moves.append(Move(f.path, str(dest), folder, reason, source_snapshot=f.source_snapshot))

    stats["seconds"] = round(time.monotonic() - t0, 2)
    stats["folders"] = len({m.folder for m in moves})
    return FastResult(moves, stats)


def build_plan_fast(files, output_root, models, known_folders=None, **kw) -> List[Move]:
    """Drop-in replacement for planner.build_plan returning just the moves."""
    return plan_files_fast(files, output_root, models, known_folders, **kw).moves

