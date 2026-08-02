/**
 * The scan screen reads a tag from a USB reader, not only from the phone.
 *
 * ADR 0014 opened the device bridge because kiosk Chromium has no Web NFC on any
 * origin, and the bench machine's only reader is a Flipper on a cable. The
 * bridge was then wired to exactly one place — the bind/verify walks inside a
 * container's edit mode — so on the bench, the commonest action in the whole
 * product (**tap a tag, go to the drawer**) was the one thing the reader could
 * not do. The scan screen collapsed to "NFC is not available on this device",
 * on a machine with a working reader plugged into it.
 *
 * The control is the second test: **no bridge, no change.** Almost every page
 * load is a phone with no bridge anywhere, and a bridge that is not running must
 * leave no empty affordance behind — ADR 0003's rule that absence is
 * communicated by absence.
 */

import { render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ScanScreen } from "./ScanScreen";

/** A machine with no bridge and no Web NFC: the bench kiosk, before this. */
class SilentSocket {
  onopen: (() => void) | null = null;
  onclose: (() => void) | null = null;
  onerror: (() => void) | null = null;
  onmessage: ((event: { data: string }) => void) | null = null;
  readyState = 0;
  constructor(readonly url: string) {}
  send(): void {}
  close(): void {
    this.readyState = 3;
  }
}

/** The same machine with the bridge up and a Flipper attached. */
class AttachingSocket extends SilentSocket {
  static taps: ((frame: string) => void)[] = [];
  constructor(url: string) {
    super(url);
    AttachingSocket.taps.push((frame) => this.onmessage?.({ data: frame }));
    queueMicrotask(() => {
      this.readyState = 1;
      this.onopen?.();
      this.onmessage?.({
        data: JSON.stringify({
          type: "station.hello",
          seq: 1,
          at: new Date(0).toISOString(),
          data: { protocol: 2, agent: "almagest-deviceagent/0.1.0", last_seq: 0 },
        }),
      });
      this.onmessage?.({
        data: JSON.stringify({
          type: "device.attached",
          seq: 2,
          at: new Date(0).toISOString(),
          data: {
            device_id: "flipper-usb:Am1n4ky",
            kind: "flipper_rpc",
            label: "Flipper Am1n4ky",
            capabilities: { reads_uid: true, reads_ndef: true, writes_ndef: true },
          },
        }),
      });
    });
  }
}

function renderScan(): void {
  render(
    <MemoryRouter>
      <ScanScreen />
    </MemoryRouter>,
  );
}

beforeEach(() => {
  AttachingSocket.taps = [];
  // No camera, no Web NFC — the bench kiosk exactly.
  vi.stubGlobal("isSecureContext", false);
  vi.stubGlobal(
    "fetch",
    vi.fn(
      async () =>
        new Response(JSON.stringify({ status: "unmatched", decoded_kind: "unknown" }), {
          status: 200,
          headers: { "content-type": "application/json" },
        }),
    ),
  );
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("reading a tag from a reader the browser cannot reach", () => {
  it("offers the attached reader instead of saying NFC is unavailable", async () => {
    vi.stubGlobal("WebSocket", AttachingSocket);
    renderScan();

    // Named, so it is obvious *which* reader is listening — there may be more
    // than one thing on the bench.
    await waitFor(() => expect(screen.getByText("Flipper Am1n4ky")).toBeTruthy());
    expect(screen.getByText(/Hold a tag to the reader/)).toBeTruthy();
    // And no button: the reader is already polling, so arming it would be
    // ceremony.
    expect(screen.queryByRole("button", { name: /Read an NFC tag/ })).toBeNull();
  });

  it("says what is missing when no reader has reported in", async () => {
    vi.stubGlobal("WebSocket", SilentSocket);
    renderScan();

    await waitFor(() => expect(screen.getByText(/No NFC reader here/)).toBeTruthy());
    // The old copy blamed the device. A USB reader is the other way to get one,
    // and on the bench it is the *only* way.
    expect(screen.getByText(/A USB reader works too/)).toBeTruthy();
    expect(screen.queryByText(/Hold a tag to the reader/)).toBeNull();
  });
});
