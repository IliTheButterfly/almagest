import { describe, expect, it } from "vitest";

import type { RotationStore } from "./orientation";
import {
  CAMERA_ROTATION_KEY,
  flipRotation,
  readCameraRotation,
  writeCameraRotation,
} from "./orientation";

function store(initial: Record<string, string> = {}): RotationStore & {
  readonly entries: Record<string, string>;
} {
  const entries: Record<string, string> = { ...initial };
  return {
    entries,
    getItem: (key) => entries[key] ?? null,
    setItem: (key, value) => {
      entries[key] = value;
    },
  };
}

/** A storage that throws on access, as some locked-down webviews do. */
const hostile: RotationStore = {
  getItem: () => {
    throw new Error("storage is disabled by policy");
  },
  setItem: () => {
    throw new Error("storage is disabled by policy");
  },
};

describe("the remembered camera rotation", () => {
  it("defaults to upright when nothing has been stored", () => {
    expect(readCameraRotation(store())).toBe(0);
  });

  it("reads back a stored half turn", () => {
    expect(readCameraRotation(store({ [CAMERA_ROTATION_KEY]: "180" }))).toBe(180);
  });

  it("round trips through a write", () => {
    const s = store();
    writeCameraRotation(s, 180);
    expect(readCameraRotation(s)).toBe(180);
    writeCameraRotation(s, 0);
    expect(readCameraRotation(s)).toBe(0);
  });

  it("treats an unrecognised value as upright rather than throwing", () => {
    // A hand-edited key, or one left behind by a build that stored degrees
    // differently. A preview the wrong way up is a nuisance; a scanner that
    // fails to render is not.
    for (const raw of ["90", "true", "", "upside-down", "180.0"]) {
      expect(readCameraRotation(store({ [CAMERA_ROTATION_KEY]: raw }))).toBe(0);
    }
  });

  it("survives a storage that throws, in both directions", () => {
    expect(readCameraRotation(hostile)).toBe(0);
    expect(() => writeCameraRotation(hostile, 180)).not.toThrow();
  });

  it("treats an absent storage as upright", () => {
    expect(readCameraRotation(null)).toBe(0);
    expect(() => writeCameraRotation(null, 180)).not.toThrow();
  });
});

describe("flipping", () => {
  it("is its own inverse", () => {
    expect(flipRotation(0)).toBe(180);
    expect(flipRotation(180)).toBe(0);
    expect(flipRotation(flipRotation(0))).toBe(0);
  });
});
