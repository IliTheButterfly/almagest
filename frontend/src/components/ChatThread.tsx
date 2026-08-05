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

import { useEffect, useState } from "react";

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

/**
 * What the model looked up, as a sentence.
 *
 * The stored value is JSON whose `arguments` is itself a JSON *string* — that is
 * the OpenAI tool-call wire shape, not a mistake — so rendering it raw gives the
 * reader `"{\"query\": \"capacitor\"}"`, backslashes and all. Nobody checks a
 * model's work through escaped JSON, and this panel exists precisely so they can.
 *
 * Falls back to the raw string if it will not parse: showing something ugly beats
 * hiding evidence.
 */
function ToolCalls({ raw }: { raw: string }) {
  let calls: { tool?: string; arguments?: string }[];
  try {
    calls = JSON.parse(raw) as { tool?: string; arguments?: string }[];
  } catch {
    return (
      <p className="mono" style={{ margin: 0, fontSize: "0.75em", wordBreak: "break-word" }}>
        {raw}
      </p>
    );
  }

  return (
    <div style={{ fontSize: "0.8em", opacity: 0.85 }}>
      {calls.map((call, index) => {
        let args = call.arguments ?? "";
        try {
          // One level of unwrapping, then rendered as `key: value` pairs — which
          // is what the reader wants to check: *what did it search for*.
          const parsed = JSON.parse(args) as Record<string, unknown>;
          args = Object.entries(parsed)
            .map(([key, value]) => `${key}: ${String(value)}`)
            .join(", ");
        } catch {
          /* leave it as it came */
        }
        return (
          <p key={index} style={{ margin: 0 }}>
            <span className="badge">{call.tool ?? "tool"}</span>{" "}
            <span className="mono" style={{ wordBreak: "break-word" }}>
              {args}
            </span>
          </p>
        );
      })}
    </div>
  );
}

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
        <ToolCalls raw={message.tool_calls_json} />
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
  // What to retry. Held separately from the composer so the box can clear on send
  // — a message that failed is already *in* the thread, so re-typing it would
  // duplicate the question rather than answer it. Retrying asks the model again
  // about the turn that is already there.
  const [retryText, setRetryText] = useState<string | null>(null);
  // Seconds since the send. Drives both the "still working" copy and the prompt
  // below, because "it has been a while" is the only thing we can honestly say —
  // the server does not report progress and a fake percentage would be a lie.
  const [waited, setWaited] = useState(0);

  // A once-a-second tick, only while a send is in flight. Cleared on unmount and
  // whenever `sending` goes false, so a thread left open does not hold a timer.
  useEffect(() => {
    if (!sending) {
      setWaited(0);
      return;
    }
    const started = Date.now();
    const timer = window.setInterval(() => {
      setWaited(Math.round((Date.now() - started) / 1000));
    }, 1000);
    return () => window.clearInterval(timer);
  }, [sending]);

  if (thread.error !== null) {
    return <ErrorBanner error={thread.error} fallback="That conversation could not be loaded." />;
  }
  if (thread.data === null) {
    return <Loading what="the conversation" />;
  }

  const detail = thread.data;

  async function send(retryOf?: string) {
    const text = retryOf ?? draft.trim();
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
      // Only offer a retry when the model failed. A message that got an answer has
      // nothing to retry, and offering it anyway invites asking twice.
      setRetryText(failed === null ? null : text);
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

      {sending && streamed === "" && (
        <div className="card" style={{ maxWidth: "min(100%, 34rem)", marginInlineEnd: "auto" }}>
          <div className="row" style={{ gap: "0.5rem", alignItems: "baseline" }}>
            <span className="badge">assistant</span>
            <span style={{ fontSize: "0.85em", opacity: 0.8 }}>
              {toolNote !== "" ? toolNote : "thinking"}
              {/* Three dots that actually move. A static "thinking…" is
                  indistinguishable from a hung request, which is precisely the
                  thing somebody is trying to work out while they stare at it. */}
              <span className="chat-ellipsis" aria-hidden="true">
                <i>.</i>
                <i>.</i>
                <i>.</i>
              </span>
            </span>
            {waited >= 5 && (
              <span style={{ fontSize: "0.8em", opacity: 0.6 }}>{waited}s</span>
            )}
          </div>

          {/* The honest explanation, offered only once it is actually slow. The
              first message after an idle period pays for weights loading into
              VRAM, and a smaller model pays much less of it. */}
          {waited >= 20 && (
            <p style={{ margin: 0, fontSize: "0.85em" }}>
              This is taking a while — the model is probably loading into VRAM.
              {modelId !== "qwen3-4b" && " A smaller model starts faster:"}
              {modelId !== "qwen3-4b" && (
                <>
                  {" "}
                  <button type="button" onClick={() => setModelId("qwen3-4b")}>
                    use Qwen3 4B next time
                  </button>
                </>
              )}
            </p>
          )}
        </div>
      )}

      {streamed !== "" && (
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
        <div className="stack">
          <ErrorBanner
            error={sendError}
            fallback="The model could not be reached, so your message has no answer yet."
          />
          {retryText !== null && (
            <button
              type="button"
              className="wide"
              disabled={sending}
              onClick={() => void send(retryText)}
            >
              {sending ? "Trying again…" : "Try again"}
            </button>
          )}
        </div>
      )}

      <div className="card stack">
        {/* `flex: 0 0 auto`. `.field` is `flex: 1 1 8rem`, which is right in a
            row of fields and wrong here: inside the composer's flex *column* it
            grows vertically and leaves a dead band between the picker and the
            box. */}
        <label className="field" style={{ flex: "0 0 auto" }}>
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
          // Wrapped, not passed directly: `send` now takes an optional retry
          // string, and a bare handler would hand it the MouseEvent as the
          // message text.
          onClick={() => void send()}
        >
          {sending ? "Sending…" : "Send"}
        </button>
      </div>
    </div>
  );
}
