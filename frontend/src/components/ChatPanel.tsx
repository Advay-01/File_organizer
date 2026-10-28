import { useEffect, useId, useLayoutEffect, useMemo, useRef, useState } from "react";
import { motion, useReducedMotion } from "motion/react";
import { ArrowUp, ChevronDown, MessageCircle } from "lucide-react";
import { Button } from "./ui/button";
import type { ChatMessage } from "../lib/workspace";
export type { ChatResult } from "../lib/workspace";

/** Animate only text actually returned by the model. Screen readers get one complete reply. */
function ReplyText({ text, animate }: { text: string; animate: boolean }) {
  const reducedMotion = useReducedMotion();
  const characters = useMemo(() => Array.from(text), [text]);
  const [visible, setVisible] = useState(animate && !reducedMotion ? 0 : characters.length);
  useEffect(() => {
    if (!animate || reducedMotion || !characters.length) { setVisible(characters.length); return; }
    let frame = 0;
    const start = performance.now();
    const duration = Math.min(1000, Math.max(280, characters.length * 9));
    setVisible(0);
    const tick = (now: number) => {
      const progress = Math.min(1, (now - start) / duration);
      setVisible(Math.ceil(progress * characters.length));
      if (progress < 1) frame = requestAnimationFrame(tick);
    };
    frame = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(frame);
  }, [animate, characters, reducedMotion]);
  return <>
    <span className="sr-only">{text}</span>
    <span aria-hidden="true" className="chat-reply-text">{characters.slice(0, visible).join("")}
      {visible < characters.length && <span className="chat-text-caret" />}
    </span>
  </>;
}

export default function ChatPanel({ disabled, busy, draft, messages, error, hasPreview, onDraftChange, onSend }: {
  disabled: boolean;
  busy: boolean;
  draft: string;
  messages: ChatMessage[];
  error: string;
  hasPreview: boolean;
  onDraftChange: (text: string) => void;
  onSend: () => Promise<void>;
}) {
  const [expanded, setExpanded] = useState(false);
  const [writing, setWriting] = useState(false);
  const input = useRef<HTMLTextAreaElement>(null);
  const log = useRef<HTMLDivElement>(null);
  const sending = useRef(false);
  const typingTimer = useRef<number | undefined>(undefined);
  const panelId = useId();
  const inputHintId = useId();
  const reducedMotion = useReducedMotion();
  const thinking = busy || messages.some((message) => message.status === "sending");

  useEffect(() => { if (expanded) input.current?.focus(); }, [expanded]);
  useEffect(() => { if (log.current) log.current.scrollTop = log.current.scrollHeight; }, [messages, thinking]);
  useEffect(() => () => window.clearTimeout(typingTimer.current), []);
  useLayoutEffect(() => {
    if (!input.current || !expanded) return;
    input.current.style.height = "auto";
    input.current.style.height = `${Math.min(input.current.scrollHeight, 160)}px`;
  }, [draft, expanded]);
  useEffect(() => { if (!draft) setWriting(false); }, [draft]);

  function updateDraft(text: string) {
    onDraftChange(text);
    window.clearTimeout(typingTimer.current);
    setWriting(Boolean(text));
    typingTimer.current = window.setTimeout(() => setWriting(false), 700);
  }
  async function send() {
    if (disabled || sending.current || !draft.trim()) return;
    sending.current = true;
    setWriting(false);
    try { await onSend(); } finally { sending.current = false; }
  }

  return <section className="ask-sortly ui-input-shell" data-expanded={expanded} aria-label="Ask Sortly">
    <button type="button" className="ask-sortly-toggle" aria-expanded={expanded} aria-controls={panelId}
      onClick={() => setExpanded((current) => !current)}>
      <MessageCircle size={17} aria-hidden="true" />
      <span>{expanded ? "Ask Sortly" : "Ask Sortly…"}</span>
      {messages.length > 0 && <span className="ask-message-count">{messages.filter((item) => item.role === "user").length}</span>}
      <ChevronDown size={15} className="ask-chevron" aria-hidden="true" />
    </button>
    <motion.div id={panelId} initial={false} animate={{ height: expanded ? "auto" : 0, opacity: expanded ? 1 : 0 }}
      transition={{ duration: reducedMotion ? 0 : .24, ease: [.22, 1, .36, 1] }}
      className="ask-sortly-body" inert={!expanded} aria-hidden={!expanded}>
      <div className="ask-sortly-content">
        <div ref={log} role="log" aria-live="polite" aria-relevant="additions" className="chat-log" data-has-messages={messages.length > 0 || thinking}>
          {messages.map((message, index) => <div key={message.id} className="chat-message" data-role={message.role}>
            <span className="chat-author">{message.role === "user" ? "You" : "Sortly"}</span>
            <p className="chat-message-text">{message.role === "assistant"
              ? <ReplyText text={message.text} animate={index === messages.length - 1} />
              : message.text}</p>
            {message.status === "queued" && <span className="chat-message-status">Queued for analysis</span>}
            {message.status === "failed" && <span className="chat-message-status text-danger">Could not send</span>}
            {Boolean(message.rules?.length) && <details className="chat-rule-details">
              <summary>{message.rules!.length} plan {message.rules!.length === 1 ? "change" : "changes"}</summary>
              <ul>{message.rules!.map((rule, ruleIndex) => <li key={ruleIndex}>
                {rule.action === "exclude" ? "Leave unchanged: " : rule.action === "rename" ? "Filenames: " : rule.action === "group_by_language" ? "Group by language: " : "Group: "}
                {rule.match.topic ?? rule.match.extension ?? rule.match.glob ?? rule.match.folder}{rule.target ? ` → ${rule.target}` : ""}
                {rule.action === "rename" && <>
                  {rule.naming?.enabled === false ? " · keep original names" : rule.naming?.enabled === true ? " · suggest supported names" : ""}
                  {rule.naming?.max_length ? ` · up to ${rule.naming.max_length} characters` : ""}
                  {rule.naming?.generic_only ? " · generic names only" : ""}
                </>}
              </li>)}</ul>
            </details>}
          </div>)}
          {thinking && <div className="chat-thinking" role="status">
            <span className="chat-dots" aria-hidden="true"><i /><i /><i /></span>
            <span>Updating your preview…</span>
          </div>}
        </div>
        {error && <p role="alert" className="ask-sortly-error">{error}</p>}
        <form className="ask-sortly-form" data-writing={writing} onSubmit={(event) => { event.preventDefault(); void send(); }}>
          <label className="ask-sortly-editor">
            <span className="sr-only">Instructions for Sortly</span>
            <textarea ref={input} value={draft} onChange={(event) => updateDraft(event.target.value)} maxLength={4000}
              disabled={disabled} rows={2} placeholder="How would you like your files organized?"
              aria-describedby={inputHintId} className="ask-sortly-input"
              onBlur={() => setWriting(false)} onKeyDown={(event) => {
                if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing) { event.preventDefault(); void send(); }
              }} />
          </label>
          <div className="ask-sortly-toolbar">
            <span id={inputHintId} className="ask-sortly-hint">
              {writing ? <><span className="chat-dots" aria-hidden="true"><i /><i /><i /></span><span>Writing</span></>
                : <span>{hasPreview ? "Enter to send" : "Enter to queue"}<span className="ask-newline-hint"> · Shift + Enter for a new line</span></span>}
            </span>
            <Button type="submit" size="sm" className="ask-sortly-send" disabled={disabled || !draft.trim()} aria-label="Send message">
              <ArrowUp size={18} strokeWidth={2.3} />
            </Button>
          </div>
        </form>
      </div>
    </motion.div>
  </section>;
}
