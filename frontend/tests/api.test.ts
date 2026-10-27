import test from "node:test";
import assert from "node:assert/strict";
import { ApiError, fetchJson, parseJsonResponse } from "../src/lib/api.ts";

test("HTTP failures preserve useful JSON and status metadata", async () => {
  const response = new Response(
    JSON.stringify({
      detail: "No files were moved.",
      status: "failed",
      moved: 0,
      undo_available: false,
    }),
    { status: 500, headers: { "content-type": "application/json" } },
  );

  await assert.rejects(
    parseJsonResponse(response, "Apply failed."),
    (error: unknown) => {
      assert.ok(error instanceof ApiError);
      assert.equal(error.status, 500);
      assert.equal(error.message, "No files were moved.");
      assert.deepEqual(error.payload, {
        detail: "No files were moved.",
        status: "failed",
        moved: 0,
        undo_available: false,
      });
      return true;
    },
  );
});

test("network failures explain the uncertain Apply outcome", async () => {
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async () => {
    throw new TypeError("Failed to fetch");
  };

  try {
    await assert.rejects(
      fetchJson("http://127.0.0.1:9/api/apply", { method: "POST" }, "Apply failed."),
      /Could not reach the local Sortly service.*Check History/i,
    );
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("an explicitly cancelled analysis retains its abort outcome", async () => {
  const originalFetch = globalThis.fetch;
  const controller = new AbortController();
  const aborted = new DOMException("Analysis cancelled", "AbortError");
  controller.abort();
  globalThis.fetch = async () => { throw aborted; };
  try {
    await assert.rejects(fetchJson("http://127.0.0.1:9/api/preview", { method: "POST", signal: controller.signal }, "Analysis failed."),
      (error: unknown) => error === aborted);
  } finally { globalThis.fetch = originalFetch; }
});
