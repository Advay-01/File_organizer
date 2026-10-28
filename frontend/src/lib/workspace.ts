export type ChatRule = {
  version?: 1;
  action: "move" | "exclude" | "rename" | "group_by_language";
  match: { topic?: string | null; extension?: string | null; glob?: string | null; folder?: string | null };
  target: string | null;
  reason: string;
  naming?: { enabled?: boolean | null; max_length?: number | null; generic_only?: boolean | null; preserve_dates?: boolean } | null;
};
export type ChatResult = { reply: string; rules: ChatRule[]; needs_clarification: string | null };
export type ChatMessage = {
  id: string;
  role: "user" | "assistant";
  text: string;
  status?: "queued" | "sending" | "sent" | "failed";
  instruction?: boolean;
  rules?: ChatRule[];
};
export function nextClarification(current: string | null, result: ChatResult): string | null {
  return result.needs_clarification || (result.rules.length ? null : current);
}

/** Reuse plan refinement before publishing a preview with the user's instructions. */
export async function applyInstructions<T extends { plan_id: string }>(
  plan: T,
  messages: ChatMessage[],
  refine: (planId: string, text: string) => Promise<{ result: ChatResult; preview: T | null }>,
  onReply: (message: ChatMessage, result: ChatResult) => void,
  signal: AbortSignal,
): Promise<{ preview: T; clarification: string | null }> {
  let current = plan;
  let clarification: string | null = null;
  for (const message of messages.filter((item) => item.role === "user" && item.instruction !== false)) {
    if (signal.aborted) throw new DOMException("Analysis cancelled", "AbortError");
    const response = await refine(current.plan_id, message.text);
    if (signal.aborted) throw new DOMException("Analysis cancelled", "AbortError");
    if (response.preview) current = response.preview;
    clarification = nextClarification(clarification, response.result);
    onReply(message, response.result);
  }
  return { preview: current, clarification };
}
