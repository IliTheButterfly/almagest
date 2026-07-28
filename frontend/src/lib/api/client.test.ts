import { afterEach, describe, expect, it, vi } from "vitest";

import { ApiError, resolveShortId, searchParts } from "./client";

function stubFetch(status: number, body: unknown): void {
  vi.stubGlobal(
    "fetch",
    vi.fn(async () =>
      new Response(JSON.stringify(body), {
        status,
        headers: { "content-type": "application/json" },
      }),
    ),
  );
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("the generated client", () => {
  it("posts the worked-example query and returns typed results", async () => {
    stubFetch(200, {
      total: 1,
      results: [
        {
          id: 1,
          name: "22uF 25V ceramic, through-hole",
          mpn: "DEMO-CAP-THT-22U",
          description: null,
          is_stub: false,
          category_id: 3,
        },
      ],
    });

    const response = await searchParts({
      category: "capacitor",
      filters: [
        { template: "mounting_type", value: "THT" },
        { template: "capacitance", value: "20-30uF" },
        { template: "capacitor_technology", value: "ceramic" },
      ],
    });

    expect(response.total).toBe(1);
    // Typed all the way through — `mpn` exists on the generated PartSummary,
    // so a backend rename would fail this file at compile time.
    expect(response.results[0]?.mpn).toBe("DEMO-CAP-THT-22U");
  });

  it("sends the request as JSON to the right path", async () => {
    stubFetch(200, { total: 0, results: [] });
    await searchParts({ filters: [{ template: "resistance", value: "4k7" }] });

    const call = vi.mocked(fetch).mock.calls[0];
    const request = call?.[0] as Request;
    expect(request.url).toContain("/api/search/parts");
    expect(request.method).toBe("POST");
  });

  it("raises with the server's detail on a 422", async () => {
    // The backend answers an uninterpretable value with a machine-readable
    // reason, which is what lets the UI say "megafarads are not a thing"
    // rather than "invalid input".
    stubFetch(422, {
      detail: { template: "capacitance", reason: "implausible", message: "1e+06 is outside…" },
    });

    await expect(
      searchParts({ filters: [{ template: "capacitance", value: "1M" }] }),
    ).rejects.toBeInstanceOf(ApiError);
  });

  it("resolves a scanned short id through the path parameter", async () => {
    stubFetch(200, {
      status: "resolved",
      target: {
        short_id: "4K7T92MQ",
        display: "BIN 4K7T-92MQ",
        entity_type: "location",
        entity_pk: 7,
        label: "Drawer A1",
        label_path: "Cabinet A / Drawer A1",
      },
    });

    const resolved = await resolveShortId("4K7T92MQ");
    expect(resolved.status).toBe("resolved");
    expect(resolved.target?.label_path).toBe("Cabinet A / Drawer A1");

    const request = vi.mocked(fetch).mock.calls[0]?.[0] as Request;
    expect(request.url).toContain("/api/resolve/4K7T92MQ");
  });
});
