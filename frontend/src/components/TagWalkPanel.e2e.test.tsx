/**
 * A whole cabinet provisioned and verified, with no hardware anywhere.
 *
 * There is no NFC reader on this setup, so the walks would otherwise ship
 * unexercised. The reader is simulated at one seam — `simulatedTagSource`, which
 * models tags the way the silicon behaves — and the server at another. Everything
 * between them is the production component.
 *
 * **What this can and cannot prove, stated plainly.** The fake server here is a
 * few dozen lines and is not the real one: the *semantics* of binding, the derived
 * cursor and the mismatch reverse-lookup are pinned by
 * `backend/tests/integration/test_nfc_end_to_end.py`, which runs the real API
 * against real migrations. What this file proves is the half that test cannot see
 * — the **sequencing the component performs**: that one tap binds exactly one
 * drawer, that the write follows the bind and the read-back follows the write,
 * that a read-back which disagrees is reported as a degraded sticker rather than a
 * failed bind, and that a tag left lying on the reader does not walk the cursor
 * down the cabinet by itself.
 *
 * That last one is the reason this file exists. A 400 ms debounce is invisible in
 * review and catastrophic when absent: one physical tap binds the cursor slot,
 * auto-advances, and binds the next slot to the same tag — silently, discovered
 * only by a verification walk weeks later.
 */

import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { TagWalk } from "./TagWalkPanel";
import type { LocationRead } from "../lib/api/client";
import { TAG_DEBOUNCE_MS } from "../lib/scan/nfc";
import { simulatedTagSource, type SimulatedTag } from "../lib/tags/simulated";

const BASE = "https://almagest.aether.lan";
const CABINET = { id: 10, name: "Cabinet A" } as unknown as LocationRead;

interface Slot {
  readonly locationId: number;
  readonly slotLabel: string;
  readonly shortId: string;
}

const SLOTS: readonly Slot[] = [
  { locationId: 101, slotLabel: "A1", shortId: "AAAAAAA1" },
  { locationId: 102, slotLabel: "A2", shortId: "AAAAAAA2" },
  { locationId: 103, slotLabel: "A3", shortId: "AAAAAAA3" },
  { locationId: 104, slotLabel: "A4", shortId: "AAAAAAA4" },
];

/** UIDs of the four tags stuck to the four drawers, in drawer order. */
const UIDS = ["04A1B2C3D4E580", "04A1B2C3D4E581", "04A1B2C3D4E582", "04A1B2C3D4E583"];

function tagsFor(overrides: Partial<Record<string, Partial<SimulatedTag>>> = {}): SimulatedTag[] {
  return UIDS.map((uid) => ({ uid, url: null, ...(overrides[uid] ?? {}) }));
}

/**
 * The server, reduced to the state the walk actually reads back.
 *
 * The cursor is derived here the same way the real one is — first slot with no
 * binding — because a fake that stored a cursor would let a component bug that
 * depends on a stored cursor pass.
 */
class FakeServer {
  readonly bindings = new Map<number, { uid: string; tagId: number; ndefState: string }>();
  readonly writeResults: { tagId: number; readBackUrl: string | null }[] = [];
  /** What the next `/undo` reports as its caveat. Null is the ordinary case. */
  undoReason: string | null = null;
  readonly checks: {
    locationId: number;
    uid: string;
    ndefUrl: string | null;
    carriesNdef: boolean;
  }[] = [];
  readonly mismatches: {
    id: number;
    locationId: number;
    scannedTagUid: string;
    scannedResolvedLocationId: number | null;
  }[] = [];
  checked = new Set<number>();
  #nextTagId = 500;

  cursorFor(kind: "provision" | "verify"): Slot | null {
    if (kind === "provision") {
      return SLOTS.find((slot) => !this.bindings.has(slot.locationId)) ?? null;
    }
    return (
      SLOTS.find((slot) => this.bindings.has(slot.locationId) && !this.checked.has(slot.locationId)) ??
      null
    );
  }

  slotOf(locationId: number): Slot {
    const slot = SLOTS.find((candidate) => candidate.locationId === locationId);
    if (slot === undefined) {
      throw new Error(`no slot ${locationId}`);
    }
    return slot;
  }

  boundElsewhere(uid: string, locationId: number): number | null {
    for (const [slotId, binding] of this.bindings) {
      if (binding.uid === uid && slotId !== locationId) {
        return slotId;
      }
    }
    return null;
  }

  cursorJson(kind: "provision" | "verify"): unknown {
    const slot = this.cursorFor(kind);
    return slot === null
      ? null
      : {
          location_id: slot.locationId,
          slot_label: slot.slotLabel,
          name: slot.slotLabel,
          label_path: `Cabinet A / ${slot.slotLabel}`,
          row_idx: null,
          col_idx: null,
          sort_order: slot.locationId,
          short_id: slot.shortId,
          has_tag: this.bindings.has(slot.locationId),
        };
  }

  provisionState(): unknown {
    return {
      session: { id: 1, root_location_id: CABINET.id, kind: "provision" },
      cursor: this.cursorJson("provision"),
      progress: {
        total_slots: SLOTS.length,
        bound: this.bindings.size,
        unbound: SLOTS.length - this.bindings.size,
        skipped: 0,
        is_complete: this.bindings.size === SLOTS.length,
      },
      undo_depth: Math.max(this.bindings.size, 1),
      undo_label: null,
    };
  }

  verifyState(): unknown {
    return {
      session: { id: 2, root_location_id: CABINET.id, kind: "verify" },
      cursor: this.cursorJson("verify"),
      progress: {
        total_tagged: this.bindings.size,
        checked: this.checked.size,
        remaining: this.bindings.size - this.checked.size,
        mismatches: this.mismatches.length,
      },
      stopped: this.mismatches.length > 0,
      mismatches: this.mismatches.map((found) => ({
        id: found.id,
        location_id: found.locationId,
        label_path: `Cabinet A / ${this.slotOf(found.locationId).slotLabel}`,
        expected_tag_uid: this.bindings.get(found.locationId)?.uid ?? null,
        scanned_tag_uid: found.scannedTagUid,
        scanned_resolved_location_id: found.scannedResolvedLocationId,
        scanned_resolved_label_path:
          found.scannedResolvedLocationId === null
            ? null
            : `Cabinet A / ${this.slotOf(found.scannedResolvedLocationId).slotLabel}`,
        created_at: "2026-07-31T00:00:00Z",
        resolved_at: null,
      })),
    };
  }

  bind(uid: string, locationId: number, move: boolean): unknown {
    const clash = this.boundElsewhere(uid, locationId);
    if (clash !== null && !move) {
      const existing = this.bindings.get(clash);
      return {
        status: "already_bound_elsewhere",
        tag: null,
        conflict: {
          tag_id: existing?.tagId ?? 0,
          tag_uid: uid,
          location_id: clash,
          slot_label: this.slotOf(clash).slotLabel,
          label_path: `Cabinet A / ${this.slotOf(clash).slotLabel}`,
        },
        state: this.provisionState(),
      };
    }
    if (clash !== null) {
      this.bindings.delete(clash);
    }
    const tagId = this.#nextTagId++;
    this.bindings.set(locationId, { uid, tagId, ndefState: "unverified" });
    const slot = this.slotOf(locationId);
    return {
      status: "bound",
      tag: {
        id: tagId,
        location_id: locationId,
        label_path: `Cabinet A / ${slot.slotLabel}`,
        tag_uid: uid,
        ndef_url: `${BASE}/s/${slot.shortId}`,
        bind_source: "phone_webnfc",
        is_read_only: false,
        written_at: "2026-07-31T00:00:00Z",
        ndef_state: "unverified",
        ndef_checked_at: null,
        last_verified_at: null,
      },
      conflict: null,
      state: this.provisionState(),
    };
  }

  check(locationId: number, uid: string, ndefUrl: string | null, carriesNdef: boolean): unknown {
    this.checks.push({ locationId, uid, ndefUrl, carriesNdef });
    const expected = this.bindings.get(locationId);
    if (expected?.uid === uid) {
      this.checked.add(locationId);
      const state = carriesNdef
        ? ndefUrl === `${BASE}/s/${this.slotOf(locationId).shortId}`
          ? "verified"
          : "degraded"
        : expected.ndefState;
      return {
        status: "match",
        location_id: locationId,
        expected_tag_uid: expected.uid,
        scanned_tag_uid: uid,
        ndef_state: state,
        mismatch: null,
        state: this.verifyState(),
      };
    }
    const belongsTo = this.boundElsewhere(uid, locationId);
    this.mismatches.push({
      id: this.mismatches.length + 1,
      locationId,
      scannedTagUid: uid,
      scannedResolvedLocationId: belongsTo,
    });
    const body = this.verifyState() as { mismatches: unknown[] };
    return {
      status: "mismatch",
      location_id: locationId,
      expected_tag_uid: expected?.uid ?? null,
      scanned_tag_uid: uid,
      ndef_state: null,
      mismatch: body.mismatches[body.mismatches.length - 1],
      state: body,
    };
  }
}

let server: FakeServer;

function stubFetch(): void {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (request: Request) => {
      const url = new URL(request.url);
      const body =
        request.method === "POST" ? ((await request.json()) as Record<string, unknown>) : {};
      const json = (payload: unknown, status = 200): Response =>
        new Response(JSON.stringify(payload), {
          status,
          headers: { "content-type": "application/json" },
        });

      if (url.pathname.endsWith("/provisioning-sessions/current")) {
        return json(server.provisionState());
      }
      if (url.pathname.endsWith("/provisioning-sessions")) {
        return json({ state: server.provisionState() }, 201);
      }
      if (url.pathname.endsWith("/undo")) {
        return json({
          undone: {
            action_kind: "bind",
            location_id: SLOTS[0]!.locationId,
            slot_label: SLOTS[0]!.slotLabel,
            label_path: `Cabinet / ${SLOTS[0]!.slotLabel}`,
            tag_uid: UIDS[0]!,
          },
          restored_tag: null,
          not_restored_reason: server.undoReason,
          state: server.provisionState(),
        });
      }
      if (url.pathname.endsWith("/verification-sessions")) {
        return json({ state: server.verifyState() }, 201);
      }
      if (url.pathname.endsWith("/bind")) {
        return json(
          server.bind(
            String(body["tag_uid"]),
            Number(body["location_id"]),
            body["move"] === true,
          ),
        );
      }
      if (url.pathname.endsWith("/check")) {
        return json(
          server.check(
            Number(body["location_id"]),
            String(body["tag_uid"]),
            (body["ndef_url"] as string | null) ?? null,
            body["carries_ndef"] === true,
          ),
        );
      }
      if (url.pathname.endsWith("/write-result")) {
        const tagId = Number(url.pathname.split("/").at(-2));
        const readBackUrl = (body["read_back_url"] as string | null) ?? null;
        server.writeResults.push({ tagId, readBackUrl });
        const binding = [...server.bindings.entries()].find(
          ([, value]) => value.tagId === tagId,
        );
        const expectedUrl =
          binding === undefined ? null : `${BASE}/s/${server.slotOf(binding[0]).shortId}`;
        const verified = readBackUrl !== null && readBackUrl === expectedUrl;
        if (binding !== undefined) {
          binding[1].ndefState = verified ? "verified" : "degraded";
        }
        return json({
          verified,
          tag: {
            id: tagId,
            location_id: binding?.[0] ?? 0,
            label_path: "Cabinet A",
            tag_uid: binding?.[1].uid ?? null,
            ndef_url: expectedUrl ?? "",
            bind_source: "phone_webnfc",
            is_read_only: false,
            written_at: "2026-07-31T00:00:00Z",
            ndef_state: verified ? "verified" : "degraded",
            ndef_checked_at: "2026-07-31T00:00:01Z",
            last_verified_at: null,
          },
        });
      }
      throw new Error(`unstubbed request: ${request.method} ${url.pathname}`);
    }),
  );
}

beforeEach(() => {
  server = new FakeServer();
  stubFetch();
});

afterEach(() => {
  vi.unstubAllGlobals();
});

/** The cursor's current drawer, as the screen says it. */
async function cursorIs(label: string): Promise<void> {
  await waitFor(() => expect(screen.getByText(label)).toBeTruthy());
}

describe("provisioning a cabinet, tap by tap", () => {
  it("binds each drawer, writes its URL, and advances by itself", async () => {
    const reader = simulatedTagSource(tagsFor());
    render(
      <TagWalk location={CABINET} kind="provision" onChanged={() => undefined} source={reader} />,
    );

    await cursorIs("A1");
    for (const [index, uid] of UIDS.entries()) {
      reader.tap(uid);
      // Each tap must finish the whole step before the next drawer is offered:
      // bind, write, read back, advance.
      await waitFor(() =>
        expect(server.bindings.get(SLOTS[index]!.locationId)?.uid).toBe(uid),
      );
    }

    await waitFor(() => expect(screen.getByTestId("provision-progress").textContent).toContain("4 of 4"));

    // The tags themselves now carry the payload — the write really happened, and
    // it is the URL of the drawer each one is bound to, not of the cursor slot at
    // the time the response came back.
    for (const [index, tag] of reader.tags.entries()) {
      expect(tag.url).toBe(`${BASE}/s/${SLOTS[index]!.shortId}`);
    }
    // ...and each read-back was reported, so nothing is left claiming an
    // unobserved success.
    expect(server.writeResults).toHaveLength(4);
    expect(server.writeResults.every((result) => result.readBackUrl !== null)).toBe(true);
  });

  it("offers the verification walk once the last drawer is bound, instead of ending nowhere", async () => {
    const reader = simulatedTagSource(tagsFor());
    const onVerifyNext = vi.fn();
    render(
      <TagWalk
        location={CABINET}
        kind="provision"
        onChanged={() => undefined}
        onVerifyNext={onVerifyNext}
        source={reader}
      />,
    );

    await cursorIs("A1");
    for (const [index, uid] of UIDS.entries()) {
      reader.tap(uid);
      await waitFor(() => expect(server.bindings.get(SLOTS[index]!.locationId)?.uid).toBe(uid));
    }
    await waitFor(() => expect(screen.getByText("Every slot has a tag")).toBeTruthy());

    // PLAN.md: a provisioning pass is "always followed by a verification pass",
    // and that one is "not optional busywork". Finishing the binds used to leave
    // a success message and no route onward — the commonest dead end there is.
    const onward = screen.getByRole("button", { name: "Verify these tags now" });
    fireEvent.click(onward);
    expect(onVerifyNext).toHaveBeenCalledTimes(1);
  });

  it("stops offering Skip once there is no slot left to skip", async () => {
    const reader = simulatedTagSource(tagsFor());
    render(
      <TagWalk location={CABINET} kind="provision" onChanged={() => undefined} source={reader} />,
    );

    await cursorIs("A1");
    expect(screen.getByRole("button", { name: "Skip this slot" }).hasAttribute("disabled")).toBe(
      false,
    );

    for (const [index, uid] of UIDS.entries()) {
      reader.tap(uid);
      await waitFor(() => expect(server.bindings.get(SLOTS[index]!.locationId)?.uid).toBe(uid));
    }
    await waitFor(() => expect(screen.getByText("Every slot has a tag")).toBeTruthy());

    // It was tappable with a null cursor and its handler silently returned — a
    // live control that does nothing, one-handed, at a cabinet.
    expect(screen.getByRole("button", { name: "Skip this slot" }).hasAttribute("disabled")).toBe(
      true,
    );
  });

  it("does not walk the cabinet on its own when a tag is left on the reader", async () => {
    const reader = simulatedTagSource(tagsFor());
    render(
      <TagWalk location={CABINET} kind="provision" onChanged={() => undefined} source={reader} />,
    );
    await cursorIs("A1");

    // A tag sitting in the field fires `reading` over and over. Without the
    // debounce this binds A1, advances, binds A2 to the same tag, and so on.
    reader.tap(UIDS[0]!);
    await waitFor(() => expect(server.bindings.size).toBe(1));
    reader.tap(UIDS[0]!);
    reader.tap(UIDS[0]!);
    reader.tap(UIDS[0]!);

    await cursorIs("A2");
    expect(server.bindings.size).toBe(1);
    expect(server.bindings.get(SLOTS[0]!.locationId)?.uid).toBe(UIDS[0]);
  });

  it("reports a sticker whose write did not take, and keeps the binding", async () => {
    // The fourth tag's user memory cannot be written. Its UID is untouched —
    // factory-locked pages 0-2 — so the drawer is still identifiable.
    const reader = simulatedTagSource(
      tagsFor({ [UIDS[0]!]: { writeFails: true } }),
    );
    render(
      <TagWalk location={CABINET} kind="provision" onChanged={() => undefined} source={reader} />,
    );
    await cursorIs("A1");

    reader.tap(UIDS[0]!);

    await waitFor(() => expect(screen.getByText(/did not take the write/i)).toBeTruthy());
    // The binding survives: a failed write is a rewrite to offer, never a
    // binding to drop.
    expect(server.bindings.get(SLOTS[0]!.locationId)?.uid).toBe(UIDS[0]);
    expect(server.bindings.get(SLOTS[0]!.locationId)?.ndefState).toBe("degraded");
    // And the server was told the truth rather than left assuming.
    expect(server.writeResults[0]?.readBackUrl).toBeNull();
  });

  it("asks before moving a tag that is already on another drawer", async () => {
    const reader = simulatedTagSource(tagsFor());
    render(
      <TagWalk location={CABINET} kind="provision" onChanged={() => undefined} source={reader} />,
    );
    await cursorIs("A1");

    reader.tap(UIDS[0]!);
    await waitFor(() => expect(server.bindings.size).toBe(1));
    await cursorIs("A2");

    // The same sticker, presented at the next drawer. Ordinary enough — you moved
    // it — so it is a question with two buttons, not an error.
    //
    // The real wait is not padding: it is the 400 ms debounce doing its job. A
    // second sighting of the same tag inside the window is a bounce, and walking
    // to the next drawer takes seconds. The test above pins the other half of
    // that same rule.
    await new Promise((resolve) => setTimeout(resolve, TAG_DEBOUNCE_MS + 60));
    reader.tap(UIDS[0]!);
    await waitFor(() => expect(screen.getByText(/Already bound to/)).toBeTruthy());
    expect(server.bindings.size).toBe(1);

    fireEvent.click(screen.getByRole("button", { name: "Move here" }));
    await waitFor(() => expect(server.bindings.get(SLOTS[1]!.locationId)?.uid).toBe(UIDS[0]));
    // Moved, not duplicated.
    expect(server.bindings.has(SLOTS[0]!.locationId)).toBe(false);
  });

  it("takes a hand-typed UID when there is no reader at all", async () => {
    render(<TagWalk location={CABINET} kind="provision" onChanged={() => undefined} />);
    await cursorIs("A1");

    fireEvent.change(screen.getByLabelText("Tag UID"), {
      target: { value: "04:a1:b2:c3:d4:e5:80" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Bind this UID" }));

    // Normalised to the same string every other reader would have produced.
    await waitFor(() => expect(server.bindings.get(SLOTS[0]!.locationId)?.uid).toBe(UIDS[0]));
  });
});

describe("the container's own tag", () => {
  /**
   * A container with no slots — a standalone bin, a shelf — could not be given a
   * tag by any screen in the app. The backend allowed it all along
   * (`resolve_target` says the cabinet "is in scope because its own tag is
   * legitimately part of the same physical walk"), but the cursor is derived
   * over the *children*, so it never landed on the container and no tap could
   * reach it.
   */
  it("binds the container itself, not the first slot", async () => {
    const reader = simulatedTagSource(tagsFor());
    render(
      <TagWalk
        location={CABINET}
        kind="provision"
        bindTarget="self"
        onChanged={() => undefined}
        source={reader}
      />,
    );

    // The cursor is the container, not A1 — which is the whole point. It is the
    // "big number" the walk puts the current target in.
    await waitFor(() =>
      expect(document.querySelector(".big-number")?.textContent).toBe(CABINET.name),
    );

    reader.tap(UIDS[0]!);

    await waitFor(() => expect(server.bindings.get(CABINET.id)?.uid).toBe(UIDS[0]));
    // And no slot was touched.
    for (const slot of SLOTS) {
      expect(server.bindings.get(slot.locationId)).toBeUndefined();
    }
  });

  it("offers no Skip, because there is nothing to skip past", async () => {
    const reader = simulatedTagSource(tagsFor());
    render(
      <TagWalk
        location={CABINET}
        kind="provision"
        bindTarget="self"
        onChanged={() => undefined}
        source={reader}
      />,
    );
    await waitFor(() =>
      expect(document.querySelector(".big-number")?.textContent).toBe(CABINET.name),
    );

    expect(screen.getByRole("button", { name: "Skip this slot" }).hasAttribute("disabled")).toBe(
      true,
    );
  });
});

describe("verifying what was bound", () => {
  /** Bind all four drawers straight into the server, as a finished walk would. */
  function preBind(): void {
    for (const [index, slot] of SLOTS.entries()) {
      server.bindings.set(slot.locationId, {
        uid: UIDS[index]!,
        tagId: 600 + index,
        ndefState: "verified",
      });
    }
  }

  it("catches two swapped stickers and names where each one belongs", async () => {
    preBind();
    // A3 and A4 have had their tags swapped by hand.
    const reader = simulatedTagSource(
      tagsFor({
        [UIDS[2]!]: { url: `${BASE}/s/${SLOTS[2]!.shortId}` },
        [UIDS[3]!]: { url: `${BASE}/s/${SLOTS[3]!.shortId}` },
      }),
    );
    render(
      <TagWalk location={CABINET} kind="verify" onChanged={() => undefined} source={reader} />,
    );
    await cursorIs("A1");

    reader.tap(UIDS[0]!);
    await cursorIs("A2");
    reader.tap(UIDS[1]!);
    await cursorIs("A3");

    // The tag now on A3 is the one that belongs to A4.
    reader.tap(UIDS[3]!);
    await waitFor(() => expect(screen.getByText(/wrong tag/i)).toBeTruthy());

    // Named, not merely flagged: "that tag belongs to A4" is actionable in a way
    // "something is wrong" is not.
    await waitFor(() => expect(screen.getAllByText(/belongs to Cabinet A \/ A4/).length).toBeGreaterThan(0));
    // And nothing was repaired — the cursor stays on A3.
    expect(server.bindings.get(SLOTS[2]!.locationId)?.uid).toBe(UIDS[2]);
  });

  it("says a right tag with a dead URL is both a match and a rewrite", async () => {
    preBind();
    // A1's sticker is the right one and its user memory is unreadable.
    const reader = simulatedTagSource(tagsFor());
    render(
      <TagWalk location={CABINET} kind="verify" onChanged={() => undefined} source={reader} />,
    );
    await cursorIs("A1");

    reader.tap(UIDS[0]!);

    await waitFor(() => expect(screen.getByText(/right tag, dead URL/i)).toBeTruthy());
    // The reader did look at user memory, and said so — that flag is what stops a
    // keyboard from making this same claim.
    expect(server.checks[0]?.carriesNdef).toBe(true);
  });

  it("never lets a typed UID mark a sticker as needing a rewrite", async () => {
    preBind();
    render(<TagWalk location={CABINET} kind="verify" onChanged={() => undefined} />);
    await cursorIs("A1");

    fireEvent.change(screen.getByLabelText("Tag UID"), { target: { value: UIDS[0]! } });
    fireEvent.click(screen.getByRole("button", { name: "Check this UID" }));

    await waitFor(() => expect(server.checks).toHaveLength(1));
    // A person reading a number off a sticker has said nothing about page 4.
    expect(server.checks[0]?.carriesNdef).toBe(false);
  });
});

describe("what an undo admits it did not do", () => {
  /**
   * These three sentences are the only place the walk tells somebody at a
   * cabinet that "undone" was not the whole truth. Before this they rendered as
   * the raw enum token — "Undone, with a caveat / prior_slot_rebound" — which
   * names nothing and offers no next action.
   *
   * The key set is checked against the generated union at compile time
   * (`UNDO_CAVEATS` is a `Record<UndoNotRestoredReason, string>`), so what is
   * left to test is that the sentence actually reaches the screen.
   */
  const REASONS = [
    "prior_slot_rebound",
    "prior_tag_bound_elsewhere",
    "slot_rebound_since",
  ] as const;

  for (const reason of REASONS) {
    it(`explains ${reason} in words rather than printing the token`, async () => {
      const reader = simulatedTagSource(tagsFor());
      server.undoReason = reason;
      render(
        <TagWalk location={CABINET} kind="provision" onChanged={() => undefined} source={reader} />,
      );
      await cursorIs("A1");

      fireEvent.click(screen.getByRole("button", { name: /^Undo/ }));

      await waitFor(() => expect(screen.getByText("Undone, with a caveat")).toBeTruthy());
      expect(screen.queryByText(reason)).toBeNull();
      // Every sentence has to name a next action, or the caveat is just bad news.
      const shown = screen.getByText(/The tag that used to be here|Nothing was removed/);
      expect(shown.textContent ?? "").toMatch(/[Uu]nbind|leave it where it is/);
    });
  }
});
