"""Benchmark metrics and isolated planner comparisons; no real model requests."""
import argparse
import importlib.util
import json
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import patch

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "benchmark-hybrid.py"
SPEC = importlib.util.spec_from_file_location("sortly_hybrid_benchmark", SCRIPT)
benchmark = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = benchmark
SPEC.loader.exec_module(benchmark)


def arguments(**changes):
    return argparse.Namespace(manifest=None, generated_files=10, real_provider=False, llm_model=None,
                              embedding_model=None, embedding_base_url="http://127.0.0.1:11434", min_files=0,
                              embedding_max_chars=12000,
                              batch_size=4, allow_rename=False, simulated_latency=0.0,
                              include_predictions=False, output=None, **changes)


def prediction(item_id, target="Science/Notes", name="notes.txt", selected=True, confidence="high"):
    return SimpleNamespace(id=item_id, target_path=target, suggested_name=name, selected=selected,
                           confidence=confidence, sufficient_for_change=selected and confidence == "high")


def reviewed(path, group=None, **kwargs):
    return benchmark.ReviewedFile(Path(path), group, **kwargs)


def manifest(tmp_path, entries=None, **changes):
    root = tmp_path / "reviewed"
    root.mkdir(exist_ok=True)
    (root / "notes.txt").write_text("Ordinary academic study notes with safe local evidence.")
    data = {"schema_version": 1, "dataset_name": "reviewed-fixture", "root": "reviewed",
            "split": "holdout", "files": entries or [{"path": "notes.txt", "group": "notes",
                                                        "accepted_targets": ["Science/Notes"]}], **changes}
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(data))
    return path, root


def test_pairwise_metrics_detect_false_merges_and_splits():
    labels = [reviewed(f"{i}.txt", group) for i, group in enumerate(["A", "A", "B", "B"])]
    actual = [prediction(i, target) for i, target in enumerate(["X/One", "X/One", "Y/Two", "X/One"])]
    metrics = benchmark.quality_metrics(labels, actual)
    assert metrics["correct_related_pairs"] == 1
    assert metrics["false_merge_pairs"] == 2 and metrics["false_split_pairs"] == 1
    assert metrics["false_merge_rate"] == pytest.approx(2 / 3)
    assert metrics["false_split_rate"] == pytest.approx(1 / 2)
    assert metrics["grouping_precision"] == pytest.approx(1 / 3)
    assert metrics["grouping_recall"] == pytest.approx(1 / 2)


def test_abstention_does_not_hide_missed_relationships():
    labels = [reviewed("first.txt", "family"), reviewed("second.txt", "family")]
    metrics = benchmark.quality_metrics(labels, [prediction(0), prediction(1, selected=False)])
    assert metrics["assignment_coverage"] == 0.5
    assert metrics["false_split_rate"] == 1
    assert metrics["false_merge_rate"] is None
    assert metrics["review_rate"] == 0.5


def test_absent_labels_are_not_reported_as_perfect_accuracy():
    metrics = benchmark.quality_metrics([reviewed("notes.txt")], [prediction(0)])
    assert metrics["approved_target_accuracy"] is None
    assert metrics["approved_name_accuracy"] is None
    assert metrics["grouping_precision"] is None and metrics["grouping_recall"] is None


def test_approved_targets_names_and_review_are_independently_scored():
    labels = [reviewed("original.txt", "notes", accepted_targets=["Science/Notes", "Education/Notes"],
                       accepted_names=["Supported.txt"], review_expected=False),
              reviewed("unknown.txt", "unclear", review_expected=True)]
    metrics = benchmark.quality_metrics(labels, [prediction(0, "SCIENCE/NOTES", "Invented.txt"),
                                                prediction(1, selected=False)])
    assert metrics["approved_target_accuracy"] == 1
    assert metrics["approved_name_accuracy"] == 0
    assert metrics["unapproved_rename_rate"] == 1
    assert metrics["review_accuracy"] == 1


def test_manifest_loads_labels_without_modifying_source(tmp_path):
    path, root = manifest(tmp_path)
    before = (root / "notes.txt").read_bytes()
    dataset = benchmark.load_manifest(path)
    assert dataset.root == root.resolve() and dataset.split == "holdout"
    assert dataset.files[0].accepted_targets == ["Science/Notes"]
    assert (root / "notes.txt").read_bytes() == before
    assert list(root.iterdir()) == [root / "notes.txt"]


@pytest.mark.parametrize("bad_path", ["../outside.txt", "..\\outside.txt", "C:\\outside.txt", "/outside.txt"])
def test_manifest_cannot_read_outside_approved_root(tmp_path, bad_path):
    path, _root = manifest(tmp_path, entries=[{"path": bad_path}])
    with pytest.raises(ValueError, match="inside"):
        benchmark.load_manifest(path)


@pytest.mark.parametrize("changes", [{"schema_version": 2}, {"files": []}, {"instructions": "unstructured"},
                                     {"dataset_name": 12}])
def test_invalid_manifest_shapes_are_rejected(tmp_path, changes):
    path, _root = manifest(tmp_path, **changes)
    with pytest.raises(ValueError):
        benchmark.load_manifest(path)


def test_manifest_rejects_duplicate_file_paths(tmp_path):
    path, _root = manifest(tmp_path, entries=[{"path": "notes.txt"}, {"path": "notes.txt"}])
    with pytest.raises(ValueError, match="duplicate"):
        benchmark.load_manifest(path)


def test_simulation_runs_all_variants_without_services_or_moves(tmp_path):
    with patch("app.core.semantic_index.LocalEmbeddingClient") as local_service:
        with patch("app.core.executor.execute_plan") as execute:
            report = benchmark.run_benchmark(arguments())
    assert report["mode"] == "simulation" and report["files_moved"] == 0
    assert "Simulation only" in report["quality_claim"]
    assert not report["real_embedding_model_load_measured"] and not report["model_download_performed"]
    assert len(report["runs"]) == 6
    assert {run["phase"] for run in report["runs"]} == {"cold-application-cache", "warm-application-cache"}
    assert all(run["status"] == "ok" for run in report["runs"])
    cold = {run["variant"]: run for run in report["runs"] if run["phase"] == "cold-application-cache"}
    assert cold["baseline"]["stats"]["llm_file_evaluations"] == 10
    assert cold["deterministic-reuse"]["stats"]["llm_file_evaluations"] < 10
    assert cold["hybrid-1"]["stats"]["embedding_status"] == "ready"
    warm = [run for run in report["runs"] if run["phase"] == "warm-application-cache"]
    assert all(run["stats"]["llm_file_evaluations"] == 0 for run in warm)
    assert all("predictions" not in run for run in report["runs"])
    local_service.assert_not_called()
    execute.assert_not_called()


def test_real_models_require_explicit_authorization():
    real = arguments()
    real.real_provider = True
    with pytest.raises(ValueError, match="explicit"):
        benchmark.run_benchmark(real)
    implicit = arguments()
    implicit.llm_model = "cloud/model"
    with pytest.raises(ValueError, match="authorize"):
        benchmark.run_benchmark(implicit)


def test_explicit_provider_mode_and_model_comparison_keep_caches_separate(tmp_path):
    path, root = manifest(tmp_path)
    args = arguments()
    args.manifest, args.real_provider, args.llm_model = path, True, "ollama/fixture"
    args.embedding_model = ["candidate-one", "candidate-two", "candidate-one"]
    observed = []

    def planner(files, output, models, **kwargs):
        cache_file = kwargs["cache"]._db.execute("PRAGMA database_list").fetchone()[2]
        observed.append((cache_file, kwargs))
        assert models == ["ollama/fixture"] and kwargs["completion_fn"] is None and kwargs["embedder"] is None
        return SimpleNamespace(suggestions=[prediction(0)], stats={"embedding_status": "disabled"})

    before = (root / "notes.txt").read_bytes()
    with patch.object(benchmark, "plan_content_intelligence", side_effect=planner):
        report = benchmark.run_benchmark(args)
    assert len(report["runs"]) == 8
    assert len({cache for cache, _kwargs in observed}) == 4
    assert observed[0][0] == observed[1][0] and observed[0][0] != observed[2][0]
    assert all(kwargs["semantic_config"].max_input_chars == args.embedding_max_chars for _cache, kwargs in observed)
    assert report["embedding_input_char_budget"] == args.embedding_max_chars
    assert not report["real_embedding_model_load_measured"]
    assert (root / "notes.txt").read_bytes() == before


def test_provider_failures_are_reported_without_echoing_private_data(tmp_path):
    args = arguments()
    with patch.object(benchmark, "plan_content_intelligence", side_effect=RuntimeError("PRIVATE CONTENT AND KEY")):
        report = benchmark.run_benchmark(args)
    assert all(run["status"] == "failed" for run in report["runs"])
    assert "PRIVATE CONTENT" not in json.dumps(report)
    assert all(run["error_type"] == "RuntimeError" for run in report["runs"])


def test_report_never_overwrites_an_existing_file(tmp_path):
    output = tmp_path / "keep.json"
    output.write_text("Keep existing report")
    with pytest.raises(SystemExit) as error:
        benchmark.main(["--output", str(output)])
    assert error.value.code == 2 and output.read_text() == "Keep existing report"


def test_generated_fixture_has_eight_groups_and_known_duplicates(tmp_path):
    dataset = benchmark.generated_dataset(tmp_path / "fixture", 100)
    assert len({item.group for item in dataset.files}) == 8
    contents = [item.path.read_bytes() for item in dataset.files]
    assert len(set(contents)) == 80


def test_real_mode_rejects_artificial_latency():
    with pytest.raises(SystemExit):
        benchmark.parse_args(["--real-provider", "--llm-model", "ollama/fixture", "--simulated-latency", "0.01"])


@pytest.mark.parametrize("budget", ["63", "32001"])
def test_embedding_character_budgets_are_bounded(budget):
    with pytest.raises(SystemExit):
        benchmark.parse_args(["--embedding-max-chars", budget])
