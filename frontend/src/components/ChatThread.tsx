/**
 * One conversation: its turns, and the box you type into.
 *
 * Shared by both surfaces (ADR 0018) — the search chat at `/chat` and the project
 * chat on a project screen. They differ in which history list they belong to and
 * in what context the agent is given, not in how a transcript looks, so there is
 * one component and a `threadId`.
 *
 * ## Tool calls are shown, never hidden
 *
 * A tool call the user cannot see is a fact they cannot check. This whole system
 * treats a model's claim as evidence rather than as an answer — the never-auto-
 * accept rule, the MPN cross-check, the review queue — and a chat surface that
 * renders only the prose would be the one place that quietly stopped doing it.
 * So `tool_calls_json` gets its own line under the turn that made it.
 *
 * ## Export is a link, not a fetch
 *
 * `chatExportUrl` returns a plain href and the browser does the rest. The point of
 * export is handing a transcript to a bigger model, which is one save-and-paste;
 * a fetch would mean holding the whole thing in memory to hand it straight back to
 * the download machinery.
 */

import { useState } from "react";

import {
  chatExportUrl,
  getChatThread,
  listChatModels,
  type ChatMessageRead,
  type ChatThreadDetail,
  type ChatModelChoice,
} from "../lib/api/client";
import { streamChat } from "../lib/chat/stream";
import { useAsync } from "../lib/hooks/useAsync";
import { ErrorBanner, Loading } from "./Feedback";

function Turn({ message }: { message: ChatMessageRead }) {
  const mine = message.role === "user";
  return (
    // `maxWidth` + `marginInlineStart:auto`, never a plain `marginLeft`: the card
    // is already full-width inside the stack, so a margin *adds* to that width and
    // pushes the whole page into a horizontal scroll — which on a phone silently
    // clips the export link and the right-hand edge of every answer.
    <div
      className="card"
      style={{
        maxWidth: "min(100%, 34rem)",
        // A flex item refuses to shrink below its content unless told to. Without
        // this the <pre> below sets the card's width and the whole page scrolls.
        minWidth: 0,
        marginInlineStart: mine ? "auto" : undefined,
        marginInlineEnd: mine ? undefined : "auto",
      }}
    >
      <div className="row" style={{ gap: "0.5rem", alignItems: "baseline" }}>
        <span className={mine ? "badge badge-accent" : "badge"}>{message.role}</span>
        {message.model !== null && (
          <span style={{ fontSize: "0.8em", opacity: 0.7 }}>{message.model}</span>
        )}
      </div>
      <p style={{ margin: 0, whiteSpace: "pre-wrap" }}>{message.content}</p>
      {message.tool_calls_json !== null && (
        // Shown rather than tucked behind a toggle. See the module docstring: the
        // point is that the reader can check what the answer was built from.
        <pre
          className="mono"
          style={{
            margin: 0,
            fontSize: "0.75em",
            // **Wrapped, not scrolled.** A sideways-scrolling box inside a
            // vertically-scrolling page is a bad gesture on a phone, and getting
            // `overflow-x: auto` to actually constrain a <pre> takes a min-width:0
            // on every ancestor — one missed and the whole page scrolls sideways,
            // silently clipping the export link. Wrapping cannot fail that way.
            whiteSpace: "pre-wrap",
            wordBreak: "break-word",
            maxWidth: "100%",
            opacity: 0.85,
          }}
        >
          {message.tool_calls_json}
        </pre>
      )}
    </div>
  );
}

export function ChatThread({ threadId }: { threadId: number }) {
  const thread = useAsync<ChatThreadDetail>(() => getChatThread(threadId), [threadId]);
  const [draft, setDraft] = useState("");
  // Which model answers. Held per mounted thread rather than persisted: the
  // *record* of which model said what already lives on each assistant turn, so
  // storing the preference too would be a second source of truth for something
  // the transcript already answers.
  const [modelId, setModelId] = useState<string>("");
  const models = useAsync<ChatModelChoice[]>(() => listChatModels(), []);
  const [sending, setSending] = useState(false);
  // The answer as it arrives. Rendered as a provisional bubble and thrown away on
  // `done`, when the thread reload brings back the persisted turn — so there is
  // never a moment where the same text exists twice.
  const [streamed, setStreamed] = useState("");
  const [toolNote, setToolNote] = useState("");
  const [sendError, setSendError] = useState<unknown>(null);

  if (thread.error !== null) {
    return <ErrorBanner error={thread.error} fallback="That conversation could not be loaded." />;
  }
  if (thread.data === null) {
    return <Loading what="the conversation" />;
  }

  const detail = thread.data;

  async function send() {
    const text = draft.trim();
    if (text === "") return;
    setSending(true);
    setSendError(null);
    try {
      // Cleared before the stream starts so the composer empties the instant the
      // turn is accepted, which is what makes it feel responsive.
      setDraft("");
      setStreamed("");
      setToolNote("");
      let failed: string | null = null;
      for await (const frame of streamChat(threadId, text, modelId || undefined)) {
        if (frame.kind === "token") {
          setStreamed((soFar) => soFar + frame.text);
        } else if (frame.kind === "tool") {
          // Shown while it happens: "looking that up" is the explanation for the
          // pause the reader is currently staring at.
          setToolNote(`looking up ${frame.tool}…`);
        } else if (frame.kind === "error") {
          failed = frame.message;
        }
      }
      setSendError(failed);
      thread.reload();
    } catch (error) {
      setSendError(error);
    } finally {
      setSending(false);
      // Dropped whether the stream finished or died. On success the reload has
      // the real turn; on failure there is no assistant turn to show, which is
      // the truth — the server persists only a whole answer.
      setStreamed("");
      setToolNote("");
    }
  }

  return (
    <div className="stack">
      <div className="row" style={{ alignItems: "baseline", gap: "0.5rem", flexWrap: "wrap" }}>
        <h2 style={{ flex: "1 1 12rem", margin: 0, minWidth: 0 }}>{detail.thread.title}</h2>
        <a className="badge" href={chatExportUrl(threadId, "md")} download>
          export .md
        </a>
        <a className="badge" href={chatExportUrl(threadId, "json")} download>
          .json
        </a>
      </div>

      {detail.messages.length === 0 && (
        <p style={{ opacity: 0.75 }}>
          Nothing said yet. Ask about what is in stock, what would substitute, or what to
          order.
        </p>
      )}

      {detail.messages.map((message) => (
        <Turn key={message.id} message={message} />
      ))}

      {(streamed !== "" || (sending && toolNote !== "")) && (
        <div className="card" style={{ maxWidth: "min(100%, 34rem)", marginInlineEnd: "auto" }}>
          <div className="row" style={{ gap: "0.5rem", alignItems: "baseline" }}>
            <span className="badge">assistant</span>
            {toolNote !== "" && (
              <span style={{ fontSize: "0.8em", opacity: 0.7 }}>{toolNote}</span>
            )}
          </div>
          <p style={{ margin: 0, whiteSpace: "pre-wrap" }}>
            {streamed}
            {/* A caret while tokens are still coming, so a slow model reads as
                working rather than stuck. */}
            {sending && <span aria-hidden="true">▍</span>}
          </p>
        </div>
      )}

      {sendError !== null && (
        <ErrorBanner error={sendError} fallback="That message could not be sent." />
      )}

      <div className="card stack">
        <label className="field">
          <span>Model</span>
          <select value={modelId} onChange={(event) => setModelId(event.target.value)}>
            <option value="">Whatever is loaded (default)</option>
            {(models.data ?? []).map((choice) => (
              <option key={choice.id} value={choice.id}>
                {choice.label}
                {choice.requires_swap ? " — needs a GPU swap" : ""}
              </option>
            ))}
          </select>
        </label>
        {/* The chosen model's own words on what it is for. Shown rather than
            tucked into a tooltip: picking well needs the sentence, and a phone
            has no hover. */}
        {modelId !== "" && (
          <p style={{ margin: 0, fontSize: "0.85em", opacity: 0.8 }}>
            {(models.data ?? []).find((choice) => choice.id === modelId)?.good_for}
          </p>
        )}
        <textarea
          value={draft}
          onChange={(event) => setDraft(event.target.value)}
          rows={3}
          placeholder="Ask something…"
          aria-label="Message"
        />
        <button
          type="button"
          className="primary wide tall"
          disabled={sending || draft.trim() === ""}
          onClick={send}
        >
          {sending ? "Sending…" : "Send"}
        </button>
      </div>
    </div>
  );
}
