"""Regressions derived from Sortly_Test_Dataset."""
from __future__ import annotations

import json
import os
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app import api
from app.core.cache import fingerprint
from app.core.content_cache import (
    ContentIntelligenceCache,
    analysis_cache_key,
    refresh_evidence_identity,
)
from app.core.content_intelligence import plan_content_intelligence
from app.core.evidence import extract_evidence
from app.core.extractor import detect_text_format, extract_content
from app.core.privacy import detect_sensitive, redact_sensitive
from app.core.safety import ScanPolicy
from app.core.scanner import scan_directory, scan_directory_with_report
from app.models.schemas import FileInfo


def file_info(path: Path) -> FileInfo:
    return FileInfo(
        path=str(path),
        filename=path.name,
        extension=path.suffix.lower(),
        size_kb=max(1, path.stat().st_size // 1024),
        date_created="2026-01-01",
    )


def model_response(rows: list[dict]) -> str:
    return json.dumps({"files": rows})


def grounded_quote(content: str) -> str:
    words = re.findall(r"[A-Za-z0-9][A-Za-z0-9._-]*", content)
    return " ".join(words[:4])


CSV_CONTENT = (
    "region,quarter,revenue_usd,units_sold,growth_pct\n"
    "North America,Q1 2026,1250000,8400,7.2\n"
    "Europe,Q1 2026,980000,6100,4.8\n"
    "Asia Pacific,Q1 2026,1410000,10250,11.5\n"
    "Latin America,Q1 2026,420000,3050,3.1\n"
    "Middle East,Q1 2026,310000,2200,5.9\n"
)

EXACT_MISMATCHES = {
    "backup_notes.xml": (
        "#!/bin/bash\n# nightly folder backup\nSRC=\"$HOME/Documents\"\n"
        "DEST=\"/mnt/backup/$(date +%F)\"\nmkdir -p \"$DEST\"\n"
        "rsync -a \"$SRC\" \"$DEST\"\necho \"Backup finished\"\n",
        ".sh",
    ),
    "budget.xlsx": (
        "Item,Planned,Actual\nRent,1200,1200\nUtilities,150,172\n"
        "Groceries,400,365\n",
        ".csv",
    ),
    "config_notes.css": (
        "terraform {\n  required_providers {\n"
        "    aws = { source = \"hashicorp/aws\", version = \"~> 5.0\" }\n"
        "  }\n}\nprovider \"aws\" { region = \"ap-south-1\" }\n"
        "resource \"aws_s3_bucket\" \"logs\" {\n"
        "  bucket = \"example-app-logs-demo\"\n}\n",
        ".tf",
    ),
    "customer_export.json": (CSV_CONTENT, ".csv"),
    "photo.png": (
        "This is plain text pretending to be a PNG image.\n"
        "It has no image header.\n",
        ".txt",
    ),
    "python_test_script.py": (
        "-- Sample strings used to test input validation\n"
        "SELECT * FROM users WHERE name = '' OR '1'='1';\n"
        "SELECT * FROM orders WHERE id = 1; DROP TABLE demo_table;--\n"
        "SELECT username, password FROM accounts WHERE id = 5 UNION SELECT 1,2;\n",
        ".sql",
    ),
    "readme_notes.md": (
        "<!DOCTYPE html>\n<html><head><title>Landing</title></head>\n"
        "<body><div id=\"app\"></div>\n"
        "<script>document.getElementById('app').textContent = "
        "'Hello from a page saved as .md';</script>\n</body></html>\n",
        ".html",
    ),
    "report_draft.docx": (CSV_CONTENT.replace("Q1", "Q2"), ".csv"),
    "terms_of_service.html": (CSV_CONTENT, ".csv"),
    "user_feedback_batch_1.json": (
        "#include <stdio.h>\n#include <string.h>\n\n"
        "/* Educational demo: unsafe copy into a fixed-size buffer */\n"
        "void greet(const char *name) {\n    char buf[16];\n"
        "    strcpy(buf, name);   // no bounds check\n"
        "    printf(\"Hello, %s\\n\", buf);\n}\n\n"
        "int main(int argc, char **argv) {\n"
        "    if (argc > 1) greet(argv[1]);\n    return 0;\n}\n",
        ".cpp",
    ),
    "weekly_team_status.md": (
        "<html><body>\n<h1>Input sanitization test page</h1>\n"
        "<script>alert('xss-test');</script>\n"
        "<img src=x onerror=\"alert('xss-test-2')\">\n</body></html>\n",
        ".html",
    ),
}


class ExactExtensionRegressionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.cache = ContentIntelligenceCache(self.root / "cache.db")
        self.saved_home = os.environ.get("ORGANIZER_HOME")
        self.saved_recent = os.environ.get("ORGANIZER_RECENT_SECONDS")
        os.environ["ORGANIZER_HOME"] = str(self.root / "home")
        os.environ["ORGANIZER_RECENT_SECONDS"] = "0"
        api.plans.clear()

    def tearDown(self):
        api.plans.clear()
        self.cache.close()
        if self.saved_home is None:
            os.environ.pop("ORGANIZER_HOME", None)
        else:
            os.environ["ORGANIZER_HOME"] = self.saved_home
        if self.saved_recent is None:
            os.environ.pop("ORGANIZER_RECENT_SECONDS", None)
        else:
            os.environ["ORGANIZER_RECENT_SECONDS"] = self.saved_recent
        self.temp.cleanup()

    def test_exact_mismatches_detect_and_suggest_correct_extension(self):
        for name, (content, expected) in EXACT_MISMATCHES.items():
            with self.subTest(name=name):
                path = self.root / name
                path.write_text(content, encoding="utf-8")
                extraction = extract_content(str(path))
                evidence = extract_evidence(file_info(path))
                self.assertEqual(extraction.detected_type, expected)
                self.assertEqual(evidence.detected_type, expected)
                self.assertTrue(evidence.extension_mismatch)

                def completion(messages):
                    row = json.loads(messages[1]["content"])["files"][0]
                    quote = grounded_quote(row["content"])
                    return model_response([{
                        "id": row["id"],
                        "category": "Test",
                        "subcategory": "Formats",
                        "confidence": "high",
                        "reasoning": "Content format is explicit.",
                        "evidence_quote": quote,
                        "filename_conflict": "extension_mismatch",
                        "suggested_name": f"corrected_{Path(name).stem}{expected}",
                        "name_evidence": quote,
                    }])

                result = plan_content_intelligence(
                    [file_info(path)],
                    str(self.root),
                    ["fake/model"],
                    cache=self.cache,
                    completion_fn=completion,
                )
                item = result.suggestions[0]
                self.assertEqual(Path(item.suggested_name).suffix, expected)
                self.assertEqual(item.selected, True)
                self.assertTrue(path.exists())

    def test_exact_mismatch_names_survive_preview_and_apply(self):
        for index, (name, (content, expected)) in enumerate(EXACT_MISMATCHES.items()):
            with self.subTest(name=name):
                case_root = self.root / f"case-{index}"
                case_root.mkdir()
                source = case_root / name
                source.write_text(content, encoding="utf-8")
                proposed = f"corrected_{Path(name).stem}{expected}"

                def completion(messages):
                    row = json.loads(messages[1]["content"])["files"][0]
                    quote = grounded_quote(row["content"])
                    return model_response([{
                        "id": row["id"],
                        "category": "Test",
                        "subcategory": "Formats",
                        "confidence": "high",
                        "reasoning": "Content format is explicit.",
                        "evidence_quote": quote,
                        "filename_conflict": "extension_mismatch",
                        "suggested_name": proposed,
                        "name_evidence": quote,
                    }])

                def real_planner(files, output_root, models, **kwargs):
                    return plan_content_intelligence(
                        files,
                        output_root,
                        models,
                        cache=self.cache,
                        completion_fn=completion,
                        **kwargs,
                    )

                with (
                    patch(
                        "app.api.provider_store.activate_runtime",
                        return_value=["fake/model"],
                    ),
                    patch(
                        "app.api.plan_content_intelligence",
                        side_effect=real_planner,
                    ),
                ):
                    preview = api.preview(api.PreviewRequest(
                        source=str(case_root),
                        rename=True,
                        reorganize=True,
                    ))
                self.assertEqual(preview.files[0].name, name)
                self.assertEqual(preview.files[0].destination_name, proposed)
                self.assertTrue(source.exists())

                result = api.apply(api.ApplyRequest(
                    plan_id=preview.plan_id,
                    files=[api.ApplyFile(
                        id=preview.files[0].id,
                        accepted=True,
                        target_path="Test/Formats",
                        destination_name=preview.files[0].destination_name,
                    )],
                ))
                self.assertEqual(result["moved"], 1)
                self.assertTrue((case_root / "Test" / "Formats" / proposed).exists())
                self.assertFalse(source.exists())

    def test_key_value_config_formats_are_not_env(self):
        samples = {
            "settings.ini": (
                "[server]\nhost = localhost\nport = 8080\n",
                ".ini",
            ),
            "settings.toml": (
                "[tool.server]\nenabled = true\nworkers = 4\n",
                ".toml",
            ),
            "config.css": (
                "provider \"aws\" {\n  region = \"ap-south-1\"\n}\n"
                "resource \"aws_s3_bucket\" \"logs\" {\n  bucket = \"demo\"\n}\n",
                ".tf",
            ),
            "runtime.env.example": (
                "API_KEY=synthetic-test-value\nDEBUG=true\n",
                ".env",
            ),
        }
        for name, (content, expected) in samples.items():
            with self.subTest(name=name):
                self.assertEqual(detect_text_format(content).extension, expected)
                source = self.root / name
                source.write_text(content, encoding="utf-8")
                self.assertEqual(
                    extract_evidence(file_info(source)).detected_type,
                    expected,
                )


class CredentialDetectionTests(unittest.TestCase):
    def test_colon_and_equals_credentials_are_detected_and_redacted(self):
        for text in (
            "password: fake-password-123",
            "PASSWORD = fake-password-123",
            "export API_TOKEN: fake-token-123",
        ):
            with self.subTest(text=text):
                kinds = {finding.kind for finding in detect_sensitive(text)}
                self.assertIn("likely_credential_assignment", kinds)
                redacted = redact_sensitive(text)
                self.assertNotIn("fake-", redacted)
                self.assertIn("[REDACTED SECRET]", redacted)

    def test_ordinary_guidance_is_not_a_credential(self):
        for text in (
            "Password: reset required before signing in.",
            "Password = use the account recovery flow.",
            "The password appears in the setup guide.",
            "password: none",
        ):
            with self.subTest(text=text):
                self.assertFalse(
                    any(
                        finding.kind == "likely_credential_assignment"
                        for finding in detect_sensitive(text)
                    )
                )


class AvailabilityAndHiddenFileTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.saved_home = os.environ.get("ORGANIZER_HOME")
        os.environ["ORGANIZER_HOME"] = str(self.root / "home")
        api.plans.clear()

    def tearDown(self):
        api.plans.clear()
        if self.saved_home is None:
            os.environ.pop("ORGANIZER_HOME", None)
        else:
            os.environ["ORGANIZER_HOME"] = self.saved_home
        self.temp.cleanup()

    def test_unavailable_content_is_unselected_and_stays_in_place(self):
        files = []
        for name, payload in (
            ("empty_file.txt", b""),
            ("random_bytes.txt", bytes(range(128)) * 16),
            ("corrupt_document.pdf", b"%PDF\x00\xff\xfe" * 20),
        ):
            path = self.root / name
            path.write_bytes(payload)
            files.append(file_info(path))
        cache = ContentIntelligenceCache(self.root / "cache.db")
        calls = []
        try:
            result = plan_content_intelligence(
                files,
                str(self.root),
                ["fake/model"],
                cache=cache,
                completion_fn=lambda _: calls.append(1) or "{}",
            )
        finally:
            cache.close()
        self.assertEqual(calls, [])
        self.assertTrue(all(not item.selected for item in result.suggestions))

        suggestions = result.suggestions
        moves = api._content_moves(suggestions, self.root)
        api.plans["unavailable"] = api.StoredPlan(self.root, moves, suggestions)
        applied = api.apply(api.ApplyRequest(
            plan_id="unavailable",
            files=[
                api.ApplyFile(id=str(item.id), accepted=item.selected)
                for item in suggestions
            ],
        ))
        self.assertEqual(applied["moved"], 0)
        self.assertTrue(all(Path(item.path).exists() for item in suggestions))

    def test_hidden_env_needs_opt_in_and_never_exposes_secret(self):
        env_file = self.root / ".env"
        env_file.write_text(
            "OPENAI_API_KEY=synthetic-private-value\n"
            "ACCESS_TOKEN=synthetic-token-value\n",
            encoding="utf-8",
        )
        (self.root / ".organizer_undo.json").write_text("[]", encoding="utf-8")
        (self.root / "notes.txt").write_text(
            "Ordinary meeting notes for the local project.",
            encoding="utf-8",
        )

        files, summary = scan_directory_with_report(
            str(self.root),
            policy=ScanPolicy(recent_seconds=0),
        )
        self.assertEqual([item.filename for item in files], ["notes.txt"])
        self.assertEqual(summary.reasons.get("hidden"), 1)
        self.assertEqual(summary.reasons.get("organizer_metadata"), 1)

        with patch.dict(
            os.environ,
            {"ORGANIZER_INCLUDE_HIDDEN_FILES": "1"},
        ):
            self.assertTrue(ScanPolicy.from_env().include_hidden_files)
            self.assertEqual(
                [item.filename for item in scan_directory(str(self.root))],
                [".env", "notes.txt"],
            )

        files, summary = scan_directory_with_report(
            str(self.root),
            policy=ScanPolicy(recent_seconds=0, include_hidden_files=True),
        )
        self.assertEqual([item.filename for item in files], [".env", "notes.txt"])
        self.assertEqual(summary.reasons.get("organizer_metadata"), 1)
        env_info = next(item for item in files if item.filename == ".env")
        evidence = extract_evidence(env_info)
        self.assertTrue(evidence.sensitive)
        self.assertEqual(evidence.detected_type, ".env")

        calls = []
        cache = ContentIntelligenceCache(self.root / "cache.db")
        try:
            result = plan_content_intelligence(
                [env_info],
                str(self.root),
                ["fake/cloud-model"],
                cache=cache,
                completion_fn=lambda messages: calls.append(messages) or "{}",
            )
        finally:
            cache.close()
        self.assertEqual(calls, [])
        item = result.suggestions[0]
        self.assertTrue(item.sensitive)
        self.assertFalse(item.selected)
        self.assertEqual(item.suggested_name, ".env")
        self.assertNotIn("synthetic-private-value", item.evidence_excerpt)
        self.assertNotIn("synthetic-token-value", item.evidence_excerpt)

    def test_explicit_hidden_env_cloud_request_is_redacted(self):
        env_file = self.root / ".env"
        env_file.write_text(
            "OPENAI_API_KEY=synthetic-private-value\n"
            "ACCESS_TOKEN=synthetic-token-value\n",
            encoding="utf-8",
        )
        sent = []
        result = plan_content_intelligence(
            [file_info(env_file)],
            str(self.root),
            ["fake/cloud-model"],
            completion_fn=lambda messages: sent.append(messages[1]["content"]) or model_response([{
                "id": 0,
                "category": "Security",
                "subcategory": "Credentials",
                "confidence": "high",
                "reasoning": "Environment credentials were explicitly reviewed.",
                "evidence_quote": "OPENAI_API_KEY",
                "filename_conflict": "none",
                "suggested_name": "",
                "name_evidence": "",
            }]),
            allow_sensitive_cloud=True,
        )
        self.assertEqual(len(sent), 1)
        self.assertNotIn("synthetic-private-value", sent[0])
        self.assertNotIn("synthetic-token-value", sent[0])
        self.assertIn("[REDACTED SECRET]", sent[0])
        self.assertTrue(result.suggestions[0].sensitive)


class DuplicateAndApplyRegressionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.saved_home = os.environ.get("ORGANIZER_HOME")
        self.saved_recent = os.environ.get("ORGANIZER_RECENT_SECONDS")
        os.environ["ORGANIZER_HOME"] = str(self.root / "home")
        os.environ["ORGANIZER_RECENT_SECONDS"] = "0"
        api.plans.clear()

    def tearDown(self):
        api.plans.clear()
        if self.saved_home is None:
            os.environ.pop("ORGANIZER_HOME", None)
        else:
            os.environ["ORGANIZER_HOME"] = self.saved_home
        if self.saved_recent is None:
            os.environ.pop("ORGANIZER_RECENT_SECONDS", None)
        else:
            os.environ["ORGANIZER_RECENT_SECONDS"] = self.saved_recent
        self.temp.cleanup()

    def test_preview_and_apply_share_collision_safe_extension_names(self):
        first = self.root / "terms_of_service.html"
        second = self.root / "report_draft.docx"
        first.write_text(CSV_CONTENT, encoding="utf-8")
        second.write_text(CSV_CONTENT, encoding="utf-8")
        prompt_names = []

        def completion(messages):
            rows = json.loads(messages[1]["content"])["files"]
            prompt_names.extend(row["current_name"] for row in rows)
            return model_response([{
                "id": row["id"],
                "category": "Finance",
                "subcategory": "Revenue",
                "confidence": "high",
                "reasoning": "Regional revenue CSV.",
                "evidence_quote": "region,quarter,revenue_usd,units_sold",
                "filename_conflict": "extension_mismatch",
                "suggested_name": "regional_revenue.csv",
                "name_evidence": "region,quarter,revenue_usd,units_sold",
            } for row in rows])

        def real_planner(files, output_root, models, **kwargs):
            return plan_content_intelligence(
                files,
                output_root,
                models,
                completion_fn=completion,
                **kwargs,
            )

        with (
            patch("app.api.provider_store.activate_runtime", return_value=["fake/model"]),
            patch("app.api.plan_content_intelligence", side_effect=real_planner),
        ):
            preview = api.preview(api.PreviewRequest(
                source=str(self.root),
                rename=True,
                reorganize=True,
            ))
        self.assertEqual(
            sorted(item.destination_name for item in preview.files),
            ["regional_revenue-1.csv", "regional_revenue.csv"],
        )
        self.assertEqual(
            sorted(prompt_names),
            ["report_draft.docx", "terms_of_service.html"],
        )
        self.assertTrue(first.exists())
        self.assertTrue(second.exists())

        result = api.apply(api.ApplyRequest(
            plan_id=preview.plan_id,
            files=[
                api.ApplyFile(
                    id=item.id,
                    accepted=True,
                    target_path="Finance/Revenue",
                    destination_name=item.destination_name,
                )
                for item in preview.files
            ],
        ))
        self.assertEqual(result["moved"], 2)
        self.assertTrue((self.root / "Finance" / "Revenue" / "regional_revenue.csv").exists())
        self.assertTrue((self.root / "Finance" / "Revenue" / "regional_revenue-1.csv").exists())
        self.assertFalse(first.exists())
        self.assertFalse(second.exists())

    def test_duplicate_content_cache_refreshes_sensitive_filename_context(self):
        public = self.root / "public.txt"
        private = self.root / "credentials_ABCDE1234F.txt"
        content = "Ordinary shared notes with no embedded credential values. " * 4
        public.write_text(content, encoding="utf-8")
        cache = ContentIntelligenceCache(self.root / "cache.db")
        calls = []

        def completion(messages):
            calls.append(json.loads(messages[1]["content"])["files"][0]["current_name"])
            return model_response([{
                "id": 0,
                "category": "Work",
                "subcategory": "Notes",
                "confidence": "high",
                "reasoning": "Ordinary notes.",
                "evidence_quote": "Ordinary shared notes",
                "filename_conflict": "none",
                "suggested_name": "",
                "name_evidence": "",
            }])

        try:
            first = plan_content_intelligence(
                [file_info(public)],
                str(self.root),
                ["fake/cloud-model"],
                cache=cache,
                completion_fn=completion,
            )
            private.write_text(content, encoding="utf-8")
            second = plan_content_intelligence(
                [file_info(private)],
                str(self.root),
                ["fake/cloud-model"],
                cache=cache,
                completion_fn=completion,
            )
        finally:
            cache.close()
        self.assertFalse(first.suggestions[0].sensitive)
        self.assertEqual(calls, ["public.txt"])
        self.assertTrue(second.suggestions[0].sensitive)
        self.assertFalse(second.suggestions[0].selected)

    def test_analysis_cache_key_includes_filename_and_parent_context(self):
        first_dir = self.root / "one"
        second_dir = self.root / "two"
        first_dir.mkdir()
        second_dir.mkdir()
        first = first_dir / "report.txt"
        second = second_dir / "report.txt"
        first.write_text("Shared report evidence. " * 10, encoding="utf-8")
        second.write_text(first.read_text(encoding="utf-8"), encoding="utf-8")
        first_evidence = extract_evidence(file_info(first))
        second_evidence = refresh_evidence_identity(first_evidence, file_info(second))
        self.assertEqual(fingerprint(first), fingerprint(second))
        self.assertNotEqual(
            analysis_cache_key(first_evidence, ["fake/model"], []),
            analysis_cache_key(second_evidence, ["fake/model"], []),
        )


if __name__ == "__main__":
    unittest.main()
