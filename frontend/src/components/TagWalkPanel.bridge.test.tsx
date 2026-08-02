/**
 * The walk offers a reader the browser cannot reach, when one is attached.
 *
 * ADR 0014 built `deviceagent`'s side and `lib/tags/bridge.ts` and joined
 * neither: `openBridge` had no production call site, so the module was
 * tree-shaken out of every build and the ADR's stated consequence — *"a laptop
 * becomes a provisioning station"* — was true of the code and false of the
 * product. On the bench kiosk, which holds the only reader in the building and
 * has no `NDEFReader`, the walk offered "Type the UID".
 *
 * These tests drive the seam that was missing. The socket is faked, because what
 * is being pinned is the component's behaviour and not the framing (which
 * `bridge.test.ts` already asserts byte for byte).
 *
 * The control is the second test: **no bridge, no change.** Almost every page
 * load is a phone with no bridge anywhere, and a bridge that is not running must
 * not leave a warning, an empty affordance or a delay behind.
 */

import { render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { TagWalk } from "./TagWalkPanel";
import type { LocationRead } from "../lib/api/client";

const CABINET = { id: 10, name: "Cabinet A" } as unknown as LocationRead;

/** A WebSocket that never opens — what a machine with no bridge looks like. */
class SilentSocket {
  static instances: SilentSocket[] = [];
  onopen: (() => void) | null = null;
  onclose: (() => void) | null = null;
  onerror: (() => void) | null = null;
  onmessage: ((event: { data: string }) => void) | null = null;
  readyState = 0;
  constructor(readonly url: string) {
    SilentSocket.instances.push(this);
  }
  send(): void {}
  close(): void {
    this.readyState = 3;
  }
}

/** A bridge that attaches one Flipper as soon as it is listened to. */
class AttachingSocket extends SilentSocket {
  static device = {
    device_id: "flipper-usb:Am1n4ky",
    kind: "flipper_rpc",
    label: "Flipper Am1n4ky",
    capabilities: { reads_uid: true, reads_ndef: true, writes_ndef: true },
  };
  constructor(url: string) {
    super(url);
    queueMicrotask(() => {
      this.readyState = 1;
      this.onopen?.();
      this.onmessage?.({
        data: JSON.stringify({
          type: "station.hello",
          seq: 1,
          at: new Date(0).toISOString(),
          data: {
            protocol: 2,
            agent: "almagest-deviceagent/0.1.0",
            last_seq: 0,
          },
        }),
      });
      this.onmessage?.({
        data: JSON.stringify({
          type: "device.attached",
          seq: 2,
          at: new Date(0).toISOString(),
          data: AttachingSocket.device,
        }),
      });
    });
  }
}

const STATE = {
  session: { id: 1, root_location_id: 10, kind: "provision" },
  cursor: {
    location_id: 101,
    slot_label: "A1",
    name: "A1",
    label_path: "Cabinet A / A1",
    row_idx: null,
    col_idx: null,
    sort_order: 101,
    short_id: "AAAAAAA1",
    has_tag: false,
  },
  progress: {
    total_slots: 1,
    bound: 0,
    unbound: 1,
    skipped: 0,
    is_complete: false,
  },
  undo_depth: 0,
  undo_label: null,
};

/**
 * Enough server to let the walk mount. Nothing here asserts on the walk itself —
 * `TagWalkPanel.e2e.test.tsx` owns that — so this only has to be *shaped* right.
 *
 * The two provisioning reads are deliberately different shapes and it matters:
 * `GET .../current` answers a bare `ProvisioningState`, while the `POST` wraps it
 * in `{state}`. Returning one body for both is how this file first failed, with
 * the component crashing on a cursor that was never there.
 */
function stubFetch(): void {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL) => {
      const url = String(
        typeof input === "object" && "url" in input ? input.url : input,
      );
      const body = url.includes("/current") ? STATE : { state: STATE };
      return new Response(JSON.stringify(body), {
        status: 200,
        headers: { "content-type": "application/json" },
      });
    }),
  );
}

afterEach(() => {
  vi.unstubAllGlobals();
  SilentSocket.instances = [];
});

describe("a reader the browser cannot reach", () => {
  it("appears in the picker when the bridge says one is attached", async () => {
    stubFetch();
    vi.stubGlobal("WebSocket", AttachingSocket);
    render(
      <TagWalk
        location={CABINET}
        kind="provision"
        onChanged={() => undefined}
      />,
    );

    // Named by the device's own label, which is what a person at the bench can
    // answer to — not a udev path and not "station reader".
    const radio = await screen.findByRole("radio", { name: /Flipper Am1n4ky/ });
    // And selected, because the alternative on a kiosk with no Web NFC is asking
    // someone to type hex off a sticker they are already holding to a reader.
    await waitFor(() => expect((radio as HTMLInputElement).checked).toBe(true));
  });

  it("connects to loopback, never to the page's origin", async () => {
    stubFetch();
    vi.stubGlobal("WebSocket", AttachingSocket);
    render(
      <TagWalk
        location={CABINET}
        kind="provision"
        onChanged={() => undefined}
      />,
    );

    await waitFor(() =>
      expect(SilentSocket.instances.length).toBeGreaterThan(0),
    );
    // The bridge is always same-machine — `agent.config` refuses a non-loopback
    // bind for the same reason. A URL derived from the origin would be a station
    // trying to reach a reader plugged into a server.
    expect(SilentSocket.instances[0]!.url).toBe("ws://127.0.0.1:8765");
  });

  it("changes nothing at all when no bridge is running", async () => {
    // The control, and the overwhelmingly common case: a phone. A bridge that is
    // not there is not an error, so there must be no warning, no extra radio and
    // no reader named.
    stubFetch();
    vi.stubGlobal("WebSocket", SilentSocket);
    render(
      <TagWalk
        location={CABINET}
        kind="provision"
        onChanged={() => undefined}
      />,
    );

    expect(
      await screen.findByRole("radio", { name: /Type the UID/ }),
    ).toBeTruthy();
    expect(screen.queryByRole("radio", { name: /Flipper/ })).toBeNull();
    expect(screen.queryByText(/bridge/i)).toBeNull();
  });
});
