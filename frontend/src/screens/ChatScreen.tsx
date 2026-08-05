/**
 * The search chat: a history list beside one conversation (ADR 0018).
 *
 * `PLAN.md`'s *fuzzy front door* — "something to level-shift 3.3 V to 5 V" when
 * you do not know the parametric query to write. Short, disposable, inventory-
 * facing.
 *
 * ## Why the two surfaces have separate lists rather than one filtered list
 *
 * This list is expected to fill with throwaway lookups. A project's list is
 * expected to hold a handful of long-running conversations somebody returns to for
 * months. Merged, the second becomes unfindable inside the first within a week —
 * which is the whole reason ADR 0018 discriminates threads by `kind` instead of
 * showing everything and letting the user squint.
 *
 * The same component renders a project's list on the project screen; only the
 * `kind` and the `projectId` differ.
 */

import { useState } from "react";

import { ChatThread } from "../components/ChatThread";
import { ErrorBanner, Loading } from "../components/Feedback";
import {
  archiveChatThread,
  createChatThread,
  listChatThreads,
  type ChatKind,
  type ChatThreadRead,
} from "../lib/api/client";
import { useAsync } from "../lib/hooks/useAsync";

export function ChatList({
  kind,
  projectId,
  emptyBlurb,
}: {
  kind: ChatKind;
  projectId?: number;
  emptyBlurb: string;
}) {
  const threads = useAsync<ChatThreadRead[]>(
    () => listChatThreads(kind, projectId),
    [kind, projectId],
  );
  const [openId, setOpenId] = useState<number | null>(null);
  const [busy, setBusy] = useState(false);
  const [actionError, setActionError] = useState<unknown>(null);

  if (threads.error !== null) {
    return <ErrorBanner error={threads.error} fallback="The conversations could not be loaded." />;
  }
  if (threads.data === null) {
    return <Loading what="the conversations" />;
  }

  const list = threads.data;

  async function start() {
    setBusy(true);
    setActionError(null);
    try {
      const created = await createChatThread(kind, projectId);
      setOpenId(created.thread.id);
      threads.reload();
    } catch (error) {
      setActionError(error);
    } finally {
      setBusy(false);
    }
  }

  async function archive(threadId: number) {
    setBusy(true);
    setActionError(null);
    try {
      await archiveChatThread(threadId, true);
      if (openId === threadId) setOpenId(null);
      threads.reload();
    } catch (error) {
      setActionError(error);
    } finally {
      setBusy(false);
    }
  }

  if (openId !== null) {
    return (
      <div className="stack">
        <button type="button" onClick={() => setOpenId(null)}>
          ← All conversations
        </button>
        <ChatThread threadId={openId} />
      </div>
    );
  }

  return (
    <div className="stack">
      {actionError !== null && (
        <ErrorBanner error={actionError} fallback="That did not work." />
      )}

      <button type="button" className="primary wide tall" disabled={busy} onClick={start}>
        New conversation
      </button>

      {list.length === 0 && <p style={{ opacity: 0.75 }}>{emptyBlurb}</p>}

      {list.map((thread) => (
        <div key={thread.id} className="card">
          <div className="row" style={{ gap: "0.5rem", alignItems: "baseline" }}>
            <button
              type="button"
              style={{ flex: 1, textAlign: "left" }}
              onClick={() => setOpenId(thread.id)}
            >
              {thread.title}
            </button>
            <span className="badge">{thread.message_count}</span>
            <button type="button" disabled={busy} onClick={() => archive(thread.id)}>
              Archive
            </button>
          </div>
        </div>
      ))}
    </div>
  );
}

export function ChatScreen() {
  return (
    <div className="stack">
      <h1>Ask</h1>
      <ChatList
        kind="search"
        emptyBlurb={
          "Nothing yet. Ask what is in stock, what would substitute for a part you cannot " +
          "find, or what a drawer is for."
        }
      />
    </div>
  );
}
