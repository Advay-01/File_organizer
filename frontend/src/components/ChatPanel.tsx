import { useRef, useState } from "react";
import { LoaderCircle, MessageCircle, Send } from "lucide-react";
import { Button } from "./ui/button";

export type ChatRule = {
  action: "move" | "exclude";
  match: { topic?: string | null; extension?: string | null; glob?: string | null };
  target: string | null;
  reason: string;
};
export type ChatResult = { reply: string; rules: ChatRule[]; needs_clarification: string | null };
type Message = { role: "user" | "assistant"; text: string; rules?: ChatRule[] };

export default function ChatPanel({ disabled, busy, onSend }: {
  disabled: boolean;
  busy: boolean;
  onSend: (message: string) => Promise<ChatResult>;
}) {
  const [draft, setDraft] = useState("");
  const [messages, setMessages] = useState<Message[]>([]);
  const [error, setError] = useState("");
  const sending = useRef(false);

  async function send() {
    const text = draft.trim();
    if (!text || disabled || sending.current) return;
    sending.current = true;
    setError("");
    setDraft("");
    setMessages((current) => [...current, { role: "user", text }]);
    try {
      const result = await onSend(text);
      setMessages((current) => [...current, {
        role: "assistant",
        text: [result.reply, result.needs_clarification].filter(Boolean).join(" "),
        rules: result.rules,
      }]);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Chat is unavailable. Try again.");
      setDraft(text);
    } finally {
      sending.current = false;
    }
  }

  return <section className="ui-panel mt-6 p-5" aria-labelledby="chat-heading">
    <h2 id="chat-heading" className="flex items-center gap-2 font-display text-lg font-semibold">
      <MessageCircle size={18} /> Refine with chat
    </h2>
    <p className="mt-2 text-xs text-ink-muted">
      Ask about a file or describe a change, such as “Group my DSA notes together” or
      “Leave Projects alone”. Changes update the preview; review and confirm to move files.
      Chat uses your configured provider and shares non-sensitive file names and short evidence excerpts.
    </p>
    <div role="log" aria-live="polite" aria-relevant="additions" className="chat-log">
      {messages.map((message, index) => <div key={index} className="chat-message" data-role={message.role}>
        <strong className="text-xs text-ink-muted">{message.role === "user" ? "You" : "Sortly"}</strong>
        <p className="mt-1 whitespace-pre-wrap text-sm">{message.text}</p>
        {Boolean(message.rules?.length) && <ul className="mt-2 space-y-1 text-xs text-ink-muted">
          {message.rules!.map((rule, ruleIndex) => <li key={ruleIndex}>
            {rule.action === "exclude" ? "Leave unchanged: " : "Group: "}
            {rule.match.topic ?? rule.match.extension ?? rule.match.glob}
            {rule.target ? ` → ${rule.target}` : ""}
          </li>)}
        </ul>}
      </div>)}
      {busy && <p className="flex items-center gap-2 text-xs text-ink-muted"><LoaderCircle size={14} className="animate-spin" /> Updating your preview…</p>}
    </div>
    {error && <p role="alert" className="mb-3 text-sm text-danger">{error}</p>}
    <form className="flex items-end gap-3" onSubmit={(event) => { event.preventDefault(); void send(); }}>
      <label className="min-w-0 flex-1">
        <span className="sr-only">Instructions for Sortly</span>
        <textarea value={draft} onChange={(event) => setDraft(event.target.value)} maxLength={4000}
          disabled={disabled} rows={2} placeholder="How would you like these files organized?"
          className="ui-input-shell w-full resize-y p-3 text-sm outline-none"
          onKeyDown={(event) => {
            if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing) {
              event.preventDefault(); void send();
            }
          }} />
      </label>
      <Button type="submit" disabled={disabled || !draft.trim()}><Send size={15} /> Send</Button>
    </form>
  </section>;
}
