"""Compare Sortly planner workload and quality without moving any files.

The default run is a deterministic simulation. Use a reviewed manifest together
with --real-provider --llm-model to evaluate actual model quality and latency.
"""
from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
import hashlib
import json
from pathlib import Path
import re
import sys
import tempfile
import time

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.core.content_cache import ContentIntelligenceCache
from app.core.content_intelligence import plan_content_intelligence
from app.core.safety import assert_safe_root, snapshot_file, validate_move_paths
from app.core.semantic_index import SemanticConfig
from app.models.schemas import FileInfo


@dataclass
class ReviewedFile:
    path: Path
    group: str | None = None
    accepted_targets: list[str] = field(default_factory=list)
    accepted_names: list[str] = field(default_factory=list)
    review_expected: bool | None = None


@dataclass
class Dataset:
    name: str
    root: Path
    files: list[ReviewedFile]
    instructions: list[str] = field(default_factory=list)
    known_folders: list[str] = field(default_factory=list)
    split: str = "unspecified"


def _string_list(value, label: str) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(item, str) or not item.strip() for item in value):
        raise ValueError(f"{label} must contain nonempty strings.")
    return value


def load_manifest(path: Path) -> Dataset:
    """A manifest is local benchmark data, never model or execution instructions."""
    if path.stat().st_size > 5 * 1024 * 1024:
        raise ValueError("Benchmark manifest is too large.")
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("schema_version") != 1:
        raise ValueError("Benchmark manifest must use schema_version 1.")
    if not isinstance(data.get("root"), str):
        raise ValueError("Benchmark manifest needs a root directory.")
    raw_root = Path(data["root"]).expanduser()
    root = assert_safe_root(raw_root if raw_root.is_absolute() else path.parent / raw_root)
    entries = data.get("files")
    if not isinstance(entries, list) or not 1 <= len(entries) <= 10000:
        raise ValueError("A benchmark needs between one and 10,000 file records.")
    files, seen = [], set()
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("path"), str):
            raise ValueError("Each benchmark file needs a relative path.")
        relative_text = entry["path"].replace("\\", "/")
        relative = Path(relative_text)
        if relative.root or relative.drive or re.match(r"^[A-Za-z]:", relative_text) or ".." in relative.parts:
            raise ValueError("Benchmark file paths must stay inside the dataset root.")
        source = root / relative
        # This performs no move or directory creation. Reuse the application's
        # protected-location and source-parent checks before reading test data.
        validate_move_paths(source, root / ("benchmark-validation-" + source.name), root)
        if source.resolve() in seen:
            raise ValueError("Benchmark manifest contains duplicate file paths.")
        seen.add(source.resolve())
        group = entry.get("group")
        if group is not None and (not isinstance(group, str) or not group.strip()):
            raise ValueError("Reviewed group IDs must be nonempty strings.")
        review_expected = entry.get("review_expected")
        if review_expected is not None and not isinstance(review_expected, bool):
            raise ValueError("review_expected must be true or false.")
        names = _string_list(entry.get("accepted_names", []), "accepted_names")
        if any(Path(name).name != name or "/" in name or "\\" in name for name in names):
            raise ValueError("Accepted names must be plain filenames.")
        files.append(ReviewedFile(source, group,
                                  _string_list(entry.get("accepted_targets", []), "accepted_targets"),
                                  names, review_expected))
    name, split = data.get("dataset_name", "reviewed-dataset"), data.get("split", "unspecified")
    if not isinstance(name, str) or not isinstance(split, str):
        raise ValueError("Dataset name and split must be strings.")
    return Dataset(name, root, files, _string_list(data.get("instructions", []), "instructions"),
                   _string_list(data.get("known_folders", []), "known_folders"), split)


_THEMES = (
    ("Marine Biology", "Coral Reefs", "reef ecosystems, corals and ocean conservation"),
    ("Robotics", "Motor Control", "actuators, joint control and motor feedback"),
    ("Urban Planning", "Public Transit", "buses, travel demand and public transport"),
    ("Astronomy", "Planetary Motion", "orbital motion, planets and gravity"),
)


def generated_dataset(root: Path, count: int = 100) -> Dataset:
    root.mkdir(parents=True)
    records = []
    for index in range(count):
        path = root / f"document_{index:04d}.txt"
        if index % 5 == 4:
            previous = records[-1]
            path.write_bytes(previous.path.read_bytes())
            records.append(ReviewedFile(path, previous.group, previous.accepted_targets,
                                        [path.name], False))
            continue
        topic, title, subject = _THEMES[(index // 2) % len(_THEMES)]
        purpose = "Lecture Notes" if index % 2 == 0 else "Exercises"
        document_title = f"{title} {purpose}"
        path.write_text(
            f"Benchmark topic: {topic};\nBenchmark purpose: {purpose};\nDocument title: {document_title};\n"
            f"Record number: {index}\nThis document discusses {subject}. "
            f"The material provides detailed {purpose.lower()} and supporting evidence about {subject}. " * 3,
            encoding="utf-8",
        )
        target = f"{topic}/{purpose}"
        records.append(ReviewedFile(path, target, [target], [path.name, document_title.replace(" ", "_") + ".txt"], False))
    return Dataset(f"generated-{count}-files", root.resolve(), records, split="synthetic-fixture")


class SimulatedEmbedder:
    """A deterministic lexical stand-in, not a trained embedding model."""

    model_identity = "benchmark:hashed-token-simulation-v1"

    def embed(self, texts, *, task):
        vectors = []
        for text in texts:
            vector = [0.0] * 64
            for token in re.findall(r"[a-z]{3,}", text.casefold()):
                number = int.from_bytes(hashlib.sha256(token.encode()).digest()[:4], "big")
                vector[number % len(vector)] += 1
            if not any(vector):
                vector[0] = 1
            vectors.append(vector)
        return vectors


class SimulatedCompletion:
    """Generated marker parsing exercises routing; it does not measure AI quality."""

    def __init__(self, latency: float = 0.0):
        self.latency = latency

    def __call__(self, messages):
        if self.latency:
            time.sleep(self.latency)
        payload = json.loads(messages[-1]["content"])
        rows = []
        for item in payload["files"]:
            content = item.get("content", "")
            topic = re.search(r"Benchmark topic:\s*([^;\r\n]+)", content)
            purpose = re.search(r"Benchmark purpose:\s*([^;\r\n]+)", content)
            title = re.search(r"Document title:\s*([^;\r\n]+)", content)
            known = topic is not None and purpose is not None
            extension = Path(item.get("current_name", "document.txt")).suffix
            rows.append({
                "id": item["id"], "category": topic.group(1) if known else "Unresolved",
                "subcategory": purpose.group(1) if known else "Review",
                "confidence": "high" if known else "insufficient",
                "reasoning": "Deterministic fixture marker parsing; not a real model judgment.",
                "evidence_quote": topic.group(0) if known else "",
                "filename_conflict": "generic" if known else "none",
                "suggested_name": title.group(1).replace(" ", "_") + extension if title else "",
                "name_evidence": title.group(1) if title else "",
            })
        return json.dumps({"files": rows})


def _pair_count(count: int) -> int:
    return count * (count - 1) // 2


def _ratio(numerator: int, denominator: int) -> float | None:
    return numerator / denominator if denominator else None


def quality_metrics(files: list[ReviewedFile], suggestions: list) -> dict:
    actual = {suggestion.id: suggestion for suggestion in suggestions}
    reference, predicted, intersections = Counter(), Counter(), Counter()
    target_total = target_right = name_total = name_right = 0
    renamed_total = renamed_wrong = review_total = review_right = assigned = 0
    reviews = 0
    for item_id, reviewed in enumerate(files):
        suggestion = actual.get(item_id)
        selected = suggestion is not None and suggestion.selected
        assigned += selected
        target = suggestion.target_path.casefold() if selected else None
        if reviewed.group is not None:
            reference[reviewed.group] += 1
            if selected:
                predicted[target] += 1
                intersections[(target, reviewed.group)] += 1
        review = (suggestion is None or not selected or suggestion.confidence in {"low", "insufficient"}
                  or not suggestion.sufficient_for_change)
        reviews += review
        if reviewed.review_expected is not None:
            review_total += 1
            review_right += review == reviewed.review_expected
        if reviewed.accepted_targets:
            target_total += 1
            target_right += selected and target in {value.casefold() for value in reviewed.accepted_targets}
        if reviewed.accepted_names:
            name_total += 1
            name_ok = suggestion is not None and suggestion.suggested_name.casefold() in {
                value.casefold() for value in reviewed.accepted_names
            }
            name_right += name_ok
            if suggestion is not None and suggestion.suggested_name != reviewed.path.name:
                renamed_total += 1
                renamed_wrong += not name_ok
    reference_pairs = sum(_pair_count(count) for count in reference.values())
    predicted_pairs = sum(_pair_count(count) for count in predicted.values())
    true_pairs = sum(_pair_count(count) for count in intersections.values())
    return {
        "labelled_group_files": sum(reference.values()), "reference_group_count": len(reference),
        "predicted_related_pairs": predicted_pairs, "reference_related_pairs": reference_pairs,
        "correct_related_pairs": true_pairs, "false_merge_pairs": predicted_pairs - true_pairs,
        "false_split_pairs": reference_pairs - true_pairs,
        "false_merge_rate": _ratio(predicted_pairs - true_pairs, predicted_pairs),
        "false_split_rate": _ratio(reference_pairs - true_pairs, reference_pairs),
        "grouping_precision": _ratio(true_pairs, predicted_pairs),
        "grouping_recall": _ratio(true_pairs, reference_pairs),
        "assignment_coverage": _ratio(assigned, len(files)), "review_rate": _ratio(reviews, len(files)),
        "review_label_count": review_total, "review_accuracy": _ratio(review_right, review_total),
        "approved_target_label_count": target_total, "approved_target_accuracy": _ratio(target_right, target_total),
        "approved_name_label_count": name_total, "approved_name_accuracy": _ratio(name_right, name_total),
        "labelled_rename_count": renamed_total, "unapproved_rename_rate": _ratio(renamed_wrong, renamed_total),
    }


def _file_infos(dataset: Dataset) -> list[FileInfo]:
    result = []
    for reviewed in dataset.files:
        path = reviewed.path
        validate_move_paths(path, dataset.root / ("benchmark-validation-" + path.name), dataset.root)
        snapshot = snapshot_file(path)
        result.append(FileInfo(path=str(path), filename=path.name, extension=path.suffix.lower(),
                               size_kb=round(snapshot.identity[2] / 1024, 2),
                               date_created=datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y-%m-%d"),
                               source_snapshot=snapshot))
    return result


def run_benchmark(args: argparse.Namespace) -> dict:
    """All planner and embedding caches live in a fresh temporary state directory."""
    if args.real_provider and not args.llm_model:
        raise ValueError("Real provider benchmarks require an explicit --llm-model.")
    if not args.real_provider and args.llm_model:
        raise ValueError("Use --real-provider to authorize real model requests.")
    with tempfile.TemporaryDirectory(prefix="sortly-hybrid-benchmark-") as runtime:
        state = Path(runtime)
        dataset = load_manifest(args.manifest.resolve()) if args.manifest else generated_dataset(state / "files", args.generated_files)
        models = list(dict.fromkeys(args.embedding_model or ["qwen3-embedding:0.6b"]))
        variants = [("baseline", False, False, models[0]), ("deterministic-reuse", True, False, models[0])]
        variants.extend((f"hybrid-{number}", True, True, model) for number, model in enumerate(models, 1))
        report = {
            "schema_version": 1, "mode": "real-provider" if args.real_provider else "simulation",
            "dataset": {"name": dataset.name, "split": dataset.split, "files": len(dataset.files),
                        "reviewed_groups": len({item.group for item in dataset.files if item.group is not None})},
            "llm_model": args.llm_model if args.real_provider else "fixture/local",
            "quality_claim": "Reviewed-label agreement; dataset size and representativeness limit generalization."
                             if args.real_provider else "Simulation only: these scores do not establish model accuracy or speed.",
            "cold_cache_scope": "Application caches only; OS, provider, and model-runtime caches are not reset.",
            "wall_measurement_scope": "Manifest file validation, hashing, and planning; not UI latency.",
            "embedding_input_char_budget": args.embedding_max_chars,
            "model_download_performed": False,
            "real_embedding_model_load_measured": args.real_provider,
            "files_moved": 0, "runs": [],
        }
        expected_fingerprints = None
        for name, duplicate_reuse, semantic_enabled, embedding_model in variants:
            cache_dir = state / name
            cache_dir.mkdir()
            config = SemanticConfig(enabled=semantic_enabled, model=embedding_model, min_files=args.min_files,
                                    base_url=args.embedding_base_url, max_input_chars=args.embedding_max_chars)
            with_cache = ContentIntelligenceCache(cache_dir / "analysis.sqlite")
            try:
                for phase in ("cold-application-cache", "warm-application-cache"):
                    started = time.perf_counter()
                    record = {"variant": name, "phase": phase, "embedding_model": embedding_model,
                              "embedding_input_char_budget": args.embedding_max_chars,
                              "duplicate_reuse": duplicate_reuse, "semantic_enabled": semantic_enabled,
                              "status": "ok"}
                    try:
                        scan_started = time.perf_counter()
                        files = _file_infos(dataset)
                        fingerprints = [file.source_snapshot.sha256 for file in files]
                        if expected_fingerprints is None:
                            expected_fingerprints = fingerprints
                        elif fingerprints != expected_fingerprints:
                            raise ValueError("The benchmark dataset changed between variants.")
                        scan_seconds = time.perf_counter() - scan_started
                        result = plan_content_intelligence(
                            files, str(dataset.root), [report["llm_model"]], known_folders=dataset.known_folders,
                            cache=with_cache, batch_size=args.batch_size, use_cache=True,
                            completion_fn=None if args.real_provider else SimulatedCompletion(args.simulated_latency),
                            allow_rename=args.allow_rename, instructions=dataset.instructions,
                            semantic_config=config, embedder=None if args.real_provider else SimulatedEmbedder(),
                            embedding_cache_path=cache_dir / "embeddings.sqlite", reuse_duplicates=duplicate_reuse,
                        )
                        record.update(scan_seconds=scan_seconds, stats=result.stats,
                                      quality=quality_metrics(dataset.files, result.suggestions))
                        if args.include_predictions:
                            record["predictions"] = [
                                {"file_id": item.id, "target": item.target_path, "name": item.suggested_name,
                                 "selected": item.selected, "confidence": item.confidence}
                                for item in result.suggestions
                            ]
                        if semantic_enabled:
                            record["semantic_backend_used"] = result.stats.get("embedding_status") == "ready"
                    except Exception as error:
                        # Do not echo provider errors, paths, credentials or document evidence.
                        record.update(status="failed", error_type=type(error).__name__,
                                      error="Planning failed; verify the explicit provider and dataset configuration.")
                    record["wall_seconds"] = time.perf_counter() - started
                    report["runs"].append(record)
            finally:
                with_cache.close()
        report["real_embedding_model_load_measured"] = args.real_provider and any(
            run.get("semantic_backend_used", False) and "embedding_model_load_seconds" in run.get("stats", {})
            for run in report["runs"]
        )
        return report


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, help="Reviewed JSON manifest; otherwise generate temporary fixtures")
    parser.add_argument("--generated-files", type=int, default=100)
    parser.add_argument("--real-provider", action="store_true", help="Explicitly permit model requests using configured credentials")
    parser.add_argument("--llm-model", help="One explicit configured model, e.g. ollama/your-model")
    parser.add_argument("--embedding-model", action="append", help="Installed local model; repeat to compare models")
    parser.add_argument("--embedding-base-url", default="http://127.0.0.1:11434")
    parser.add_argument("--embedding-max-chars", type=int, default=12000,
                        help="Evidence-card character budget shared by every embedding model in the comparison")
    parser.add_argument("--min-files", type=int, default=75)
    parser.add_argument("--batch-size", type=int, default=8)
    parser.add_argument("--allow-rename", action="store_true")
    parser.add_argument("--simulated-latency", type=float, default=0.0, help="Artificial seconds per fixture request; never a measured provider latency")
    parser.add_argument("--include-predictions", action="store_true", help="Include potentially private derived names/targets in output")
    parser.add_argument("--output", type=Path, help="Write a new JSON report; existing files are never overwritten")
    args = parser.parse_args(argv)
    if not 1 <= args.generated_files <= 10000 or not 1 <= args.batch_size <= 100:
        parser.error("File and batch counts are outside their supported ranges.")
    if not 0 <= args.min_files <= 100000:
        parser.error("Embedding minimum file count is outside its supported range.")
    if not 64 <= args.embedding_max_chars <= 32000:
        parser.error("Embedding character budget must be between 64 and 32000.")
    if not 0 <= args.simulated_latency <= 1:
        parser.error("Simulated latency must be between zero and one second.")
    if args.real_provider and args.simulated_latency:
        parser.error("Artificial latency is available only in simulation mode.")
    if args.output and args.output.exists():
        parser.error("The report already exists; choose a new output path.")
    return args


def main(argv=None) -> int:
    args = parse_args(argv)
    try:
        report = run_benchmark(args)
        payload = json.dumps(report, indent=2, ensure_ascii=False)
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            with args.output.open("x", encoding="utf-8") as output:
                output.write(payload + "\n")
            print("Benchmark report written. No files were moved.")
        else:
            print(payload)
        return int(any(run["status"] != "ok" for run in report["runs"]))
    except Exception as error:
        print(f"Benchmark failed ({type(error).__name__}); no files were moved.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
