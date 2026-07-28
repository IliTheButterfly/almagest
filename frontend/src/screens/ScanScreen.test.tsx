/**
 * Graceful degradation on the scan screen.
 *
 * ADR 0001: `getUserMedia` and `NDEFReader` are *absent* over plain HTTP and Web NFC
 * is absent on iOS and the kiosk permanently. So the requirement is not "handle the
 * error" — there is no error to handle — it is **never render an affordance that
 * cannot work**, and always leave a manual path.
 */

import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ScanScreen } from "./ScanScreen";

function renderScan(): void {
  render(
    <MemoryRouter initialEntries={["/scan"]}>
      <ScanScreen />
    </MemoryRouter>,
  );
}

/** What a browser on `http://<lan-ip>:5173` actually exposes: neither API. */
function plainHttpBrowser(): void {
  vi.stubGlobal("isSecureContext", false);
  vi.stubGlobal("navigator", { userAgent: "test" });
  vi.stubGlobal("NDEFReader", undefined);
}

/** An https origin with a camera and no Web NFC — iOS, or desktop Chromium. */
function cameraOnlyBrowser(): void {
  vi.stubGlobal("isSecureContext", true);
  vi.stubGlobal("navigator", {
    userAgent: "test",
    // Never resolves: jsdom has no real capture device, and what is under test is
    // which affordances get rendered, not what a live stream does.
    mediaDevices: { getUserMedia: vi.fn(() => new Promise<MediaStream>(() => undefined)) },
  });
  vi.stubGlobal("NDEFReader", undefined);
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("with neither a camera nor NFC", () => {
  it("renders no viewfinder and no camera button", () => {
    plainHttpBrowser();
    renderScan();

    expect(document.querySelector("video")).toBeNull();
    expect(screen.queryByRole("button", { name: /start camera/i })).toBeNull();
    expect(screen.queryByRole("button", { name: /read an nfc tag/i })).toBeNull();
  });

  it("says why, and names the origin that would work", () => {
    plainHttpBrowser();
    renderScan();

    expect(screen.getByText(/No camera here/)).toBeTruthy();
    // Both notices name it — the camera's and the NFC one.
    expect(screen.getAllByText(/https:\/\/almagest\.lan/).length).toBeGreaterThan(0);
  });

  it("still offers the manual path, so the app is fully usable", () => {
    plainHttpBrowser();
    renderScan();

    expect(screen.getByText("Or type it")).toBeTruthy();
    expect(screen.getByRole("button", { name: /look up/i })).toBeTruthy();
  });
});

describe("with a camera but no NFC", () => {
  it("offers the camera and explains the NFC gap without a dead button", () => {
    cameraOnlyBrowser();
    renderScan();

    expect(document.querySelector("video")).not.toBeNull();
    expect(screen.queryByRole("button", { name: /read an nfc tag/i })).toBeNull();
    expect(screen.getByText(/NFC is not available on this device/)).toBeTruthy();
  });

  it("keeps the manual path alongside the camera", () => {
    cameraOnlyBrowser();
    renderScan();
    expect(screen.getByText("Or type it")).toBeTruthy();
  });
});
