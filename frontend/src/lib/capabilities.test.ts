import { describe, expect, it } from "vitest";

import {
  cameraNotice,
  detectCapabilities,
  nfcNotice,
  type CapabilityScope,
} from "./capabilities";

const secureWithBoth: CapabilityScope = {
  isSecureContext: true,
  navigator: { mediaDevices: { getUserMedia: () => undefined } },
  NDEFReader: function NDEFReader() {
    /* stand-in for the constructor */
  },
};

/** What Chrome actually looks like on `http://192.168.1.10:5173` from a phone. */
const plainHttp: CapabilityScope = { isSecureContext: false, navigator: {} };

/** Safari on iOS at an https origin: a camera, and no Web NFC at any URL. */
const iosHttps: CapabilityScope = {
  isSecureContext: true,
  navigator: { mediaDevices: { getUserMedia: () => undefined } },
};

describe("capability detection", () => {
  it("finds both when the browser exposes both", () => {
    expect(detectCapabilities(secureWithBoth)).toEqual({
      secureContext: true,
      camera: true,
      nfc: true,
    });
  });

  it("finds neither over plain HTTP, where the APIs do not exist", () => {
    expect(detectCapabilities(plainHttp)).toEqual({
      secureContext: false,
      camera: false,
      nfc: false,
    });
  });

  it("probes the API rather than inferring it from the scheme", () => {
    // iOS is a secure context with a camera and no Web NFC. Inferring NFC from
    // https would render a button that does nothing.
    expect(detectCapabilities(iosHttps)).toEqual({
      secureContext: true,
      camera: true,
      nfc: false,
    });
  });

  it("does not mistake a non-function mediaDevices for a camera", () => {
    const scope: CapabilityScope = {
      isSecureContext: true,
      navigator: { mediaDevices: {} },
    };
    expect(detectCapabilities(scope).camera).toBe(false);
  });
});

describe("degrading visibly", () => {
  it("says nothing when the capability is present", () => {
    const capabilities = detectCapabilities(secureWithBoth);
    expect(cameraNotice(capabilities)).toBeNull();
    expect(nfcNotice(capabilities)).toBeNull();
  });

  it("explains the secure-context requirement and names the right origin", () => {
    const notice = cameraNotice(detectCapabilities(plainHttp));
    expect(notice).not.toBeNull();
    expect(notice).toContain("https://almagest.lan");
    expect(notice).toContain("localhost");
    // The most important part: there is no permission to grant, so telling the
    // user to check their permissions would send them in circles.
    expect(notice).toContain("no permission to grant");
  });

  it("always points at the manual path", () => {
    expect(cameraNotice(detectCapabilities(plainHttp))).toMatch(/type the code/i);
    expect(cameraNotice(detectCapabilities({ isSecureContext: true }))).toMatch(/manual/i);
  });

  it("calls Web NFC a permanent platform limit rather than a setting", () => {
    const notice = nfcNotice(detectCapabilities(iosHttps));
    expect(notice).toContain("Chrome-for-Android only");
    expect(notice).toContain("permanent platform limit");
    // And says what still works: the tag itself opens its /s/ URL.
    expect(notice).toContain("/s/");
  });

  it("blames the scheme, not the platform, when NFC is missing over http", () => {
    expect(nfcNotice(detectCapabilities(plainHttp))).toContain("secure context");
  });
});
