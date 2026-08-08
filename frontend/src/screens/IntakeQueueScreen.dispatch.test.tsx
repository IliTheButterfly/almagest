/**
 * The intake panel's proposal section (ADR 0021).
 *
 * What these tests are really guarding is the *wording and the affordances*, because
 * that is where a propose-never-assert rule actually gets broken. The backend can be
 * perfectly correct and the screen can still say "CF14JT100K" in a way that reads as a
 * fact, or hide the losers, or colour an unreadable photograph red so the real failures
 * stop standing out.
 *
 * Specifically:
 *
 * * the quoted `source_text` is on screen next to every proposal, because that is what a
 *   person checks against the picture — ADR 0021 records the quote catching a
 *   fabrication that no confidence score would have;
 * * the second and third readings are rendered, not hidden behind a disclosure;
 * * `unidentified` is not styled as breakage;
 * * accepting a candidate calls back with **that candidate's** part id and nothing else,
 *   so the only write is the ordinary resolve.
 */

import { fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";

import type { IdentityCandidateRead, PendingIntakeRead } from "../lib/api/client";
import { IdentityProposal } from "./IntakeQueueScreen";

function candidate(overrides: Partial<IdentityCandidateRead> = {}): IdentityCandidateRead {
  return {
    mpn: "CF14JT100K",
    manufacturer: "Stackpole",
    package: "axial",
    confidence: 0.79,
    source_text: "MFR PART NO: CF14JT100K",
    note: null,
    part_id: 42,
    rank: 0,
    provider: "local-ollama",
    model: "qwen3-vl:8b",
    created_at: "2026-08-07T00:00:00Z",
    ...overrides,
  };
}

function entry(overrides: Partial<PendingIntakeRead> = {}): PendingIntakeRead {
  return {
    id: 1,
    client_op_id: "op-1",
    raw_payload: "capture:abc",
    symbology: null,
    decoded_kind: null,
    scan_event_id: null,
    capture_id: 7,
    mpn: null,
    manufacturer: null,
    supplier_part_number: null,
    date_code: null,
    lot_code: null,
    quantity_milli: null,
    part_id: null,
    resolved_part_id: null,
    note: null,
    device_id: null,
    status: "pending",
    queued_at: null,
    created_at: "2026-08-07T00:00:00Z",
    resolved_at: null,
    dispatch_state: "not_requested",
    dispatch_attempts: 0,
    dispatch_error: null,
    dispatch_label_kind: null,
    identity_candidates: [],
    ...overrides,
  };
}

function draw(
  overrides: Partial<PendingIntakeRead> = {},
  // Resolves, because `onChoose` is typed `Promise<void>` and the row chains `.finally`
  // on it to clear its own busy state. A bare `vi.fn()` returns undefined and would make
  // the test fail for a reason the component does not have.
  onChoose = vi.fn().mockResolvedValue(undefined),
) {
  render(
    <MemoryRouter>
      <IdentityProposal entry={entry(overrides)} onChanged={vi.fn()} onChoose={onChoose} />
    </MemoryRouter>,
  );
  return onChoose;
}

describe("nobody has asked for a read", () => {
  it("offers the button and says what it costs", () => {
    draw();
    expect(screen.getByRole("button", { name: /ask a model to read it/i })).toBeTruthy();
    // The cost is stated on the screen, not only in an ADR. This is the whole reason the
    // queue is opt-in, and a user who does not know that cannot make the decision.
    expect(screen.getByText(/takes the graphics card away/i)).toBeTruthy();
  });

  it("shows no proposals, because there are none", () => {
    draw();
    expect(screen.queryByText(/suggestions, best first/i)).toBeNull();
  });
});

describe("a model has proposed identities", () => {
  const proposed = {
    dispatch_state: "proposed" as const,
    dispatch_label_kind: "bag",
    identity_candidates: [
      candidate(),
      candidate({
        mpn: "CFI4JT100K",
        manufacturer: null,
        package: null,
        confidence: 0.35,
        source_text: "CFI4JT100K",
        note: "the same line, read the other way",
        part_id: 43,
        rank: 1,
      }),
    ],
  };

  it("prints the quoted characters beside every reading", () => {
    draw(proposed);
    // Both quotes, because the quote is the thing being checked against the picture.
    // Matched inside the `.mono` quote span rather than by a bare regex: the misread
    // string is also this candidate's part number, so a loose match finds two elements
    // and passes for the wrong reason.
    const quotes = Array.from(document.querySelectorAll("span.mono")).map(
      (node) => node.textContent ?? "",
    );
    expect(quotes.some((text) => text.includes("MFR PART NO: CF14JT100K"))).toBe(true);
    expect(quotes.some((text) => text.includes("\u201cCFI4JT100K\u201d"))).toBe(true);
  });

  it("renders the losers too, not just the best guess", () => {
    draw(proposed);
    expect(screen.getByText("CF14JT100K")).toBeTruthy();
    expect(screen.getByText("CFI4JT100K")).toBeTruthy();
  });

  it("says these are suggestions and warns what the model confuses them with", () => {
    draw(proposed);
    expect(screen.getByText(/suggestions, best first/i)).toBeTruthy();
    // The measured failure mode, in the user's words: the model read an FCC ID as a part
    // number at 0.95. Somebody skimming has to know that is the thing to look for.
    expect(screen.getByText(/certification number or a URL/i)).toBeTruthy();
  });

  it("describes the confidence as the model's own opinion rather than a measurement", () => {
    draw(proposed);
    expect(screen.getByText(/the model rated its own reading 0\.79/i)).toBeTruthy();
    // No progress bar, no meter: it is not calibrated and must not look like it is.
    expect(document.querySelector("progress")).toBeNull();
    expect(document.querySelector("meter")).toBeNull();
  });

  it("accepting one calls back with that candidate's part id", () => {
    const onChoose = draw(proposed);
    const buttons = screen.getAllByRole("button", { name: /this is it/i });
    expect(buttons.length).toBe(2);

    // Destructured rather than indexed: `noUncheckedIndexedAccess` types `buttons[1]` as
    // possibly undefined, and the length assertion above does not narrow it.
    const [, second] = buttons;
    expect(second).toBeDefined();
    fireEvent.click(second as HTMLElement);
    // The *second* candidate's stub, so a mis-wired index would be caught rather than
    // passing because both happened to be the same id.
    expect(onChoose).toHaveBeenCalledWith(43);
    expect(onChoose).toHaveBeenCalledTimes(1);
  });

  it("cannot be accepted when no stub was minted for it", () => {
    draw({
      dispatch_state: "proposed",
      identity_candidates: [candidate({ part_id: null })],
    });
    expect(screen.queryByRole("button", { name: /this is it/i })).toBeNull();
    expect(screen.getByText(/no stub part was created/i)).toBeTruthy();
  });
});

describe("the model could not read it", () => {
  it("says so as a normal outcome and points at the fix", () => {
    draw({ dispatch_state: "unidentified" });
    expect(screen.getByText(/that is not a fault/i)).toBeTruthy();
    // The fix is another photograph, which is the distinction between UNIDENTIFIED and
    // FAILED made visible to the person who has to act on it.
    expect(screen.getByText(/take another picture/i)).toBeTruthy();
  });

  it("is not badged as breakage", () => {
    draw({ dispatch_state: "unidentified" });
    const badge = screen.getByText("nothing legible");
    expect(badge.className).not.toContain("badge-bad");
  });

  it("still offers a re-read", () => {
    draw({ dispatch_state: "unidentified" });
    expect(screen.getByRole("button", { name: /read it again/i })).toBeTruthy();
  });
});

describe("the run itself broke", () => {
  it("is badged as breakage and shows the reason", () => {
    draw({ dispatch_state: "failed", dispatch_error: "the model server refused" });
    expect(screen.getByText("read broke").className).toContain("badge-bad");
    expect(screen.getByText(/the model server refused/i)).toBeTruthy();
  });
});

describe("a read is in flight", () => {
  it("offers a way out of the queue rather than a second request", () => {
    draw({ dispatch_state: "pending" });
    expect(screen.getByRole("button", { name: /take it out of the queue/i })).toBeTruthy();
    expect(screen.queryByRole("button", { name: /ask a model to read it/i })).toBeNull();
  });

  it("says a worker is on it once claimed", () => {
    draw({ dispatch_state: "claimed" });
    expect(screen.getByText(/being read now/i)).toBeTruthy();
  });
});
