import test from "node:test";
import assert from "node:assert/strict";
import { describeUndoResult } from "../src/lib/undo.ts";

test("source and destination conflicts keep recovery available", () => {
  const feedback = describeUndoResult({
    status: "applied",
    restored: 0,
    skipped: [{ file: "both.txt", reason: "original location is already occupied" }],
  });

  assert.equal(feedback.tone, "warning");
  assert.equal(feedback.recoveryAvailable, true);
  assert.match(feedback.message, /could not restore any files/i);
  assert.match(feedback.message, /conflicted or disappeared/i);
});

test("neither path existing is not reported as no run", () => {
  const feedback = describeUndoResult({
    status: "applied",
    restored: 0,
    skipped: [{ file: "gone.txt", reason: "file is no longer there" }],
  });

  assert.equal(feedback.tone, "warning");
  assert.equal(feedback.recoveryAvailable, true);
  assert.doesNotMatch(feedback.message, /no organization run/i);
  assert.match(feedback.message, /Review History/i);
});

test("a fully restored run clears recovery access", () => {
  const feedback = describeUndoResult({
    status: "undone",
    restored: 2,
    skipped: [],
  });

  assert.equal(feedback.tone, "success");
  assert.equal(feedback.recoveryAvailable, false);
  assert.equal(feedback.message, "Restored 2 files.");
});

test("an empty history is distinct from an undo conflict", () => {
  const feedback = describeUndoResult({
    status: "none",
    restored: 0,
    skipped: [],
  });

  assert.equal(feedback.tone, "success");
  assert.equal(feedback.recoveryAvailable, false);
  assert.equal(feedback.message, "There was no organization run to undo.");
});
