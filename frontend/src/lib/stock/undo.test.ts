import { describe, expect, it } from "vitest";

import { offersUndo, undoSecondsLeft, UNDO_WINDOW_MS } from "./undo";

describe("the undo window", () => {
  it("offers an undo for a movement that was just recorded", () => {
    expect(offersUndo({ replayed: false })).toBe(true);
  });

  it("suppresses the undo when the server replayed a stored answer", () => {
    // `replayed: true` means no new movement was written — the work happened on an
    // earlier request with the same key, so the eight-second window closed then.
    // Offering an undo here would misrepresent what would be reversed.
    expect(offersUndo({ replayed: true })).toBe(false);
  });

  it("offers an undo when the field is absent", () => {
    // A server predating the field only ever did the work.
    expect(offersUndo({})).toBe(true);
  });

  it("counts down whole seconds and never below zero", () => {
    expect(undoSecondsLeft(0)).toBe(8);
    expect(undoSecondsLeft(1)).toBe(8);
    expect(undoSecondsLeft(1_000)).toBe(7);
    expect(undoSecondsLeft(7_500)).toBe(1);
    expect(undoSecondsLeft(UNDO_WINDOW_MS)).toBe(0);
    expect(undoSecondsLeft(UNDO_WINDOW_MS + 5_000)).toBe(0);
  });

  it("uses the eight seconds the workflow specifies", () => {
    expect(UNDO_WINDOW_MS).toBe(8_000);
  });
});
