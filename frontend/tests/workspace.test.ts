import test from "node:test";
import assert from "node:assert/strict";
import { applyInstructions, nextClarification, type ChatMessage, type ChatResult } from "../src/lib/workspace.ts";

const move: ChatResult = { reply: "Updated the plan.", rules: [{ action: "move", match: { extension: ".txt" }, target: "Research/Surveys", reason: "user request" }], needs_clarification: null };
const answer: ChatResult = { reply: "The content describes a survey.", rules: [], needs_clarification: null };
const user = (id: string, text: string): ChatMessage => ({ id, role: "user", text, instruction: true });

test("optional chat makes no refinement requests when there are no instructions", async () => {
  const plan = { plan_id: "original" };
  const result = await applyInstructions(plan, [], async () => { throw new Error("Unexpected LLM call"); }, () => {}, new AbortController().signal);
  assert.equal(result.preview, plan);
  assert.equal(result.clarification, null);
});

test("multiple instructions chain real plan IDs while questions do not replace the preview", async () => {
  const calls: string[][] = [];
  const replies: string[] = [];
  const messages: ChatMessage[] = [user("1", "Group surveys"), { id: "2", role: "assistant", text: "Previous reply" }, user("3", "Leave receipts alone"), { ...user("4", "Why?"), instruction: false }];
  const result = await applyInstructions({ plan_id: "first" }, messages, async (id, text) => {
    calls.push([id, text]);
    return calls.length === 1 ? { result: move, preview: { plan_id: "second" } } : { result: answer, preview: null };
  }, (message) => replies.push(message.id), new AbortController().signal);
  assert.deepEqual(calls, [["first", "Group surveys"], ["second", "Leave receipts alone"]]);
  assert.deepEqual(replies, ["1", "3"]);
  assert.equal(result.preview.plan_id, "second");
});

test("clarification blocks approval until a rule-producing reply resolves it", () => {
  const question = { reply: "Which course?", rules: [], needs_clarification: "Which course?" };
  assert.equal(nextClarification(null, question), "Which course?");
  assert.equal(nextClarification("Which course?", answer), "Which course?");
  assert.equal(nextClarification("Which course?", move), null);
});

test("cancelled instruction processing discards a late server response", async () => {
  const controller = new AbortController();
  let published = false;
  await assert.rejects(applyInstructions({ plan_id: "first" }, [user("1", "Group surveys")], async () => {
    controller.abort();
    return { result: move, preview: { plan_id: "stale" } };
  }, () => { published = true; }, controller.signal), { name: "AbortError" });
  assert.equal(published, false);
});

test("an LLM error stops the instruction chain without publishing a partial final preview", async () => {
  let calls = 0;
  await assert.rejects(applyInstructions({ plan_id: "first" }, [user("1", "one"), user("2", "two")], async () => {
    calls++;
    throw new Error("Provider unavailable");
  }, () => {}, new AbortController().signal), /Provider unavailable/);
  assert.equal(calls, 1);
});
