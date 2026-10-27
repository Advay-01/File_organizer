import test from "node:test";
import assert from "node:assert/strict";
import {
  describeSkips,
  filterReviewGroups,
  getFileReviewStatus,
  getFilenameExtensionChange,
  getInitialGroupLimit,
  getReviewBatchMode,
  shouldExpandGroupsInitially,
  shouldShowReviewTools,
  skipReasonLabel,
  summarizeFileWarning,
  summarizeGroup,
  summarizeReview,
  type ReviewFile,
} from "../src/lib/review.ts";

function file(overrides: Partial<ReviewFile> = {}): ReviewFile {
  return {
    id: "1",
    name: "notes.txt",
    target_path: "Study/Notes",
    destination_name: "calculus_notes.txt",
    category: "Study/Notes",
    confidence: "high",
    evidence_sufficient: true,
    extraction_status: "sufficient",
    filename_conflict: "none",
    warnings: [],
    sensitive: false,
    selected: true,
    ...overrides,
  };
}

test("extension changes distinguish type corrections from ordinary and case-only renames", () => {
  assert.deepEqual(getFilenameExtensionChange("notes.html", "notes.csv"), { from: ".html", to: ".csv" });
  assert.equal(getFilenameExtensionChange("draft.txt", "plant_notes.txt"), null);
  assert.equal(getFilenameExtensionChange("notes.PDF", "Notes.pdf"), null);
  assert.deepEqual(getFilenameExtensionChange("report.v2.html", "report.v2.csv"), { from: ".html", to: ".csv" });
  assert.deepEqual(getFilenameExtensionChange("README", "README.txt"), { from: "", to: ".txt" });
  assert.deepEqual(getFilenameExtensionChange(".env", "environment.txt"), { from: "", to: ".txt" });
});

test("small, medium, and large batches use progressive disclosure", () => {
  assert.equal(getReviewBatchMode(30), "small");
  assert.equal(getReviewBatchMode(300), "medium");
  assert.equal(getReviewBatchMode(3000), "large");
  assert.equal(shouldExpandGroupsInitially(100), true);
  assert.equal(shouldExpandGroupsInitially(101), false);
  assert.equal(shouldShowReviewTools(30), false);
  assert.equal(shouldShowReviewTools(300), true);
  assert.equal(getInitialGroupLimit(1024), 40);
});

test("review counts distinguish ready, attention, security, and excluded files", () => {
  const files = [
    file({ id: "ready" }),
    file({ id: "uncertain", confidence: "low" }),
    file({ id: "private", sensitive: true, warnings: ["Potential sensitive content detected."] }),
    file({ id: "excluded", selected: false }),
  ];
  const stats = summarizeReview(files, {});
  assert.deepEqual(stats, {
    total: 4,
    selected: 3,
    excluded: 1,
    ready: 1,
    attention: 1,
    security: 1,
  });
});

test("filtering preserves all review state and exposes matching groups", () => {
  const files = [
    file({ id: "1", name: "budget.csv", target_path: "Finance/Reports", category: "Finance/Reports" }),
    file({ id: "2", name: "notes.md", target_path: "Study/Notes" }),
    file({ id: "3", name: "secret.env", sensitive: true, target_path: "Study/Notes" }),
  ];
  const edits = {
    "1": { accepted: false, target_path: "Finance/Reports", destination_name: "budget.csv" },
  };
  const groups: Array<[string, ReviewFile[]]> = [
    ["Finance/Reports", [files[0]]],
    ["Study/Notes", [files[1], files[2]]],
  ];
  const filtered = filterReviewGroups(groups, edits, "notes", "all");
  assert.deepEqual(filtered.map(([target]) => target), ["Study/Notes"]);
  assert.equal(filtered[0][1].length, 2);
  assert.equal(edits["1"].accepted, false);

  const security = filterReviewGroups(groups, edits, "", "security");
  assert.deepEqual(security[0][1].map((item) => item.id), ["3"]);
});

test("recovered diagnostics stay detailed instead of becoming the primary warning", () => {
  const item = file({
    filename_conflict: "extension_mismatch",
    warnings: ["File extension .json disagreed with its JSON parser; readable text was inspected."],
  });
  assert.match(summarizeFileWarning(item), /file type may be misleading/i);
  assert.equal(getFileReviewStatus(item), "attention");
});

test("security takes precedence over ordinary attention", () => {
  const item = file({
    confidence: "low",
    sensitive: true,
    warnings: ["Potential credential content detected."],
  });
  assert.equal(getFileReviewStatus(item), "security");
  assert.match(summarizeFileWarning(item), /sensitive content/i);
});

test("ordinary clearer names and untouched uncertain files do not inflate review counts", () => {
  const files = [
    file({ id: "name", filename_conflict: "generic" }),
    file({ id: "unreadable", selected: false, confidence: "low", extraction_status: "unavailable", evidence_sufficient: false }),
    file({ id: "real-mismatch", filename_conflict: "extension_mismatch" }),
  ];
  assert.deepEqual(files.map((item) => getFileReviewStatus(item)), ["ready", "excluded", "attention"]);
  assert.equal(summarizeFileWarning(files[1]), "");
  assert.equal(summarizeReview(files, {}).attention, 1);
});

test("group summaries and large initial rendering stay bounded", () => {
  const items = Array.from({ length: 10_000 }, (_, index) => file({
    id: String(index),
    name: `file-${index}.txt`,
    target_path: `Folder-${Math.floor(index / 10)}/Notes`,
  }));
  const groups = new Map<string, ReviewFile[]>();
  for (const item of items) {
    groups.set(item.target_path, [...(groups.get(item.target_path) ?? []), item]);
  }
  const groupEntries = [...groups.entries()];
  const summary = summarizeGroup(groupEntries[0][1], {});
  assert.deepEqual(summary, { attention: 0, security: 0 });
  assert.equal(shouldExpandGroupsInitially(items.length), false);
  assert.equal(getInitialGroupLimit(items.length), 40);
});

test("skipped files use plain-language notices and never imply analysis", () => {
  const message = describeSkips({
    skipped: 3,
    reasons: {
      hidden: 1,
      locked_or_unreadable: 1,
      system_file: 1,
      protected_or_special_directory: 2,
    },
    truncated: false,
  });
  assert.match(message, /3 files were left untouched and not analyzed/i);
  assert.match(message, /hidden files/i);
  assert.match(message, /could not be opened/i);
  assert.match(message, /system files/i);
  assert.doesNotMatch(message, /protected_or_special_directory/);
  assert.equal(skipReasonLabel("organizer_metadata"), "Sortly metadata files");
  assert.equal(skipReasonLabel("custom_limit"), "custom limit");

  const truncated = describeSkips({
    skipped: 1,
    reasons: { file_count_limit: 1 },
    truncated: true,
  });
  assert.match(truncated, /^1 file was left untouched and not analyzed/i);
  assert.match(truncated, /file-count limit was reached/i);
});
