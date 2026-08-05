/**
 * Reading the chat SSE stream.
 *
 * `fetch` with a body reader rather than `EventSource`, for one blunt reason:
 * `EventSource` can only issue a GET. Sending a turn is a POST — it appends a
 * message — and a GET that mutates the transcript would be retried by every proxy
 * and prefetched by anything that thinks it is being helpful.
 *
 * ## Frames
 *
 * `tool` zero or more, then `token` many, then exactly one of `done` or `error`.
 * A stream that ends without `done` did **not** land an assistant turn — the
 * server persists only after the answer is whole, so the honest thing for the
 * client to do is discard its provisional bubble and leave the question standing.
 * That matches what actually happened, and asking again is the recovery.
 */

export type ChatFrame =
  | { kind: "tool"; tool: string; arguments: string }
  | { kind: "token"; text: string }
  | { kind: "done"; messageId: number | null; model: string }
  | { kind: "error"; message: string };

/**
 * POST a turn and yield frames as they arrive.
 *
 * Parsing is deliberately hand-rolled and small: SSE is `event:`/`data:` pairs
 * separated by a blank line, and a library for that would be a dependency for
 * fifteen lines. The one subtlety is that a chunk can split a frame in half, so
 * the tail is carried over rather than parsed and dropped.
 */
export async function* streamChat(
  threadId: number,
  content: string,
  model?: string,
): AsyncGenerator<ChatFrame> {
  const response = await fetch(`/api/chat/threads/${threadId}/stream`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ content, model: model ?? null }),
  });

  if (!response.ok || response.body === null) {
    yield { kind: "error", message: `the server answered ${response.status}` };
    return;
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });

    // Frames are blank-line separated. Anything after the last separator is a
    // partial frame and stays in the buffer for the next chunk.
    const frames = buffer.split("\n\n");
    buffer = frames.pop() ?? "";

    for (const frame of frames) {
      let event = "message";
      let data = "";
      for (const line of frame.split("\n")) {
        if (line.startsWith("event:")) event = line.slice(6).trim();
        else if (line.startsWith("data:")) data += line.slice(5).trim();
      }
      if (data === "") continue;

      let payload: Record<string, unknown>;
      try {
        payload = JSON.parse(data) as Record<string, unknown>;
      } catch {
        // A frame we cannot read is not worth killing the stream over: the next
        // one usually carries the rest of the answer.
        continue;
      }

      if (event === "token") {
        yield { kind: "token", text: String(payload.text ?? "") };
      } else if (event === "tool") {
        yield {
          kind: "tool",
          tool: String(payload.tool ?? ""),
          arguments: String(payload.arguments ?? ""),
        };
      } else if (event === "done") {
        yield {
          kind: "done",
          messageId: (payload.message_id as number | null) ?? null,
          model: String(payload.model ?? ""),
        };
      } else if (event === "error") {
        yield { kind: "error", message: String(payload.message ?? "the model failed") };
      }
    }
  }
}
