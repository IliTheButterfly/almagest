/**
 * The datasheet section on the part screen: the one-tap view, the upload path,
 * and set-primary/remove — see the component's own docstring for why the
 * "view" button uses `window.open` rather than a router `<Link>`, and why
 * upload goes through a raw `fetch` rather than the typed client.
 */

import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { DocumentLinkRead } from "../lib/api/client";
import { DocumentsPanel } from "./DocumentsPanel";

const PART_ID = 42;
const SHA_A = "a".repeat(64);
const SHA_B = "b".repeat(64);
const SHA_UPLOAD = "c".repeat(64);

function makeLink(overrides: Partial<DocumentLinkRead> = {}): DocumentLinkRead {
  return {
    role: "datasheet",
    is_primary: true,
    created_at: "2026-01-01T00:00:00Z",
    document: {
      id: 1,
      sha256: SHA_A,
      kind: "datasheet",
      media_type: "application/pdf",
      byte_size: 2048,
      page_count: null,
      source_url: null,
      original_filename: "STM32F103.pdf",
      created_at: "2026-01-01T00:00:00Z",
      url: `/api/documents/${SHA_A}`,
    },
    ...overrides,
  };
}

interface Call {
  readonly url: string;
  readonly method: string;
}

let links: DocumentLinkRead[];
const calls: Call[] = [];

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

function stubApi(): void {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
      const request = input instanceof Request ? input : new Request(input, init);
      const url = new URL(request.url);
      calls.push({ url: url.pathname + url.search, method: request.method });

      if (url.pathname === `/api/parts/${PART_ID}/documents` && request.method === "GET") {
        return jsonResponse({ part_id: PART_ID, links });
      }

      if (url.pathname === "/api/documents" && request.method === "POST") {
        const bytes = await request.arrayBuffer();
        const role = (url.searchParams.get("role") ?? "datasheet") as DocumentLinkRead["role"];
        const newLink = makeLink({
          role,
          is_primary: true,
          document: {
            ...makeLink().document,
            sha256: SHA_UPLOAD,
            byte_size: bytes.byteLength,
            original_filename: url.searchParams.get("filename"),
            url: `/api/documents/${SHA_UPLOAD}`,
          },
        });
        // Uploading forces primary within the role, same as the real service.
        links = [newLink, ...links.filter((existing) => existing.role !== role)];
        return jsonResponse({
          document: newLink.document,
          created: true,
          deduplicated: false,
          link: newLink,
        });
      }

      if (url.pathname === `/api/parts/${PART_ID}/documents` && request.method === "POST") {
        const body = JSON.parse(await request.text()) as { sha256: string; role: string };
        links = links.map((existing) =>
          existing.role === body.role
            ? { ...existing, is_primary: existing.document.sha256 === body.sha256 }
            : existing,
        );
        const promoted = links.find((existing) => existing.document.sha256 === body.sha256);
        if (promoted === undefined) {
          return jsonResponse({ detail: { reason: "unknown_document", message: "no" } }, 404);
        }
        return jsonResponse({ link: promoted, created: false });
      }

      if (
        url.pathname.startsWith(`/api/parts/${PART_ID}/documents/`) &&
        request.method === "DELETE"
      ) {
        const sha256 = url.pathname.slice(url.pathname.lastIndexOf("/") + 1);
        links = links.filter((existing) => existing.document.sha256 !== sha256);
        return jsonResponse({ detached: 1, promoted: [] });
      }

      throw new Error(`unstubbed request: ${request.method} ${url.pathname}`);
    }),
  );
}

beforeEach(() => {
  calls.length = 0;
  links = [];
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("no documents attached", () => {
  it("says so instead of showing the view button", async () => {
    stubApi();
    render(<DocumentsPanel partId={PART_ID} />);

    expect(await screen.findByText(/None yet/)).toBeTruthy();
    expect(screen.queryByRole("button", { name: "View datasheet" })).toBeNull();
  });
});

describe("a part with a primary datasheet", () => {
  it("offers a one-tap view straight to the redirect route, not a rendered screen", async () => {
    stubApi();
    links = [makeLink()];
    const openSpy = vi.spyOn(window, "open").mockImplementation(() => null);
    render(<DocumentsPanel partId={PART_ID} />);

    const button = await screen.findByRole("button", { name: "View datasheet" });
    fireEvent.click(button);

    expect(openSpy).toHaveBeenCalledWith(`/api/parts/${PART_ID}/datasheet`, "_blank", "noopener");
  });

  it("lists it with its role and primary badge", async () => {
    stubApi();
    links = [makeLink()];
    render(<DocumentsPanel partId={PART_ID} />);

    const filename = await screen.findByText("STM32F103.pdf");
    const row = filename.closest("li");
    expect(row).not.toBeNull();
    // Scoped to the badge, not the role/kind `<select>`s below, which render
    // the same word as an `<option>`.
    expect(row?.querySelector("span.badge")?.textContent).toBe("datasheet");
    expect(row?.textContent).toContain("primary");
    // The primary link gets no "Set primary" button — there is nothing to
    // promote it to.
    expect(screen.queryByRole("button", { name: "Set primary" })).toBeNull();
  });
});

describe("promoting and removing", () => {
  it("promotes a non-primary sibling via attach, and the badge moves", async () => {
    stubApi();
    links = [
      makeLink({ is_primary: true }),
      makeLink({
        is_primary: false,
        document: { ...makeLink().document, sha256: SHA_B, original_filename: "rev2.pdf" },
      }),
    ];
    render(<DocumentsPanel partId={PART_ID} />);

    await screen.findByText("STM32F103.pdf");
    fireEvent.click(screen.getByRole("button", { name: "Set primary" }));

    await waitFor(() =>
      expect(
        calls.find(
          (call) => call.method === "POST" && call.url === `/api/parts/${PART_ID}/documents`,
        ),
      ).toBeDefined(),
    );
    // The list reloaded from the (now-updated) stub: the promoted link is now
    // primary (and lost its own "Set primary" button) while the sibling it
    // demoted gained one in its place — promoting a role's primary is a
    // trade, not an addition, so there is still exactly one such button.
    await waitFor(() => expect(screen.getAllByRole("button", { name: "Set primary" })).toHaveLength(1));
    expect(await screen.findByText("rev2.pdf")).toBeTruthy();
    const badges = screen.getAllByText("primary");
    expect(badges).toHaveLength(1);
    // It moved onto rev2.pdf's row specifically, not merely "somewhere".
    const rev2Row = screen.getByText("rev2.pdf").closest("li");
    expect(rev2Row?.textContent).toContain("primary");
  });

  it("detaches a document and drops it from the list without touching the blob", async () => {
    stubApi();
    links = [makeLink()];
    render(<DocumentsPanel partId={PART_ID} />);

    await screen.findByText("STM32F103.pdf");
    fireEvent.click(screen.getByRole("button", { name: "Remove" }));

    await waitFor(() =>
      expect(
        calls.some(
          (call) => call.method === "DELETE" && call.url === `/api/parts/${PART_ID}/documents/${SHA_A}`,
        ),
      ).toBe(true),
    );
    await waitFor(() => expect(screen.queryByText("STM32F103.pdf")).toBeNull());
  });
});

describe("uploading", () => {
  it("posts the bytes with role, kind and part id in the query string, then shows the result", async () => {
    stubApi();
    render(<DocumentsPanel partId={PART_ID} />);
    await screen.findByText(/None yet/);

    const file = new File([new Uint8Array([1, 2, 3, 4])], "new-sheet.pdf", {
      type: "application/pdf",
    });
    const input = screen.getByLabelText(/Upload a PDF, PNG or JPEG/);
    fireEvent.change(input, { target: { files: [file] } });

    await waitFor(() =>
      expect(calls.some((call) => call.method === "POST" && call.url.startsWith("/api/documents?"))).toBe(
        true,
      ),
    );
    const uploadCall = calls.find(
      (call) => call.method === "POST" && call.url.startsWith("/api/documents?"),
    );
    const query = new URLSearchParams(uploadCall?.url.split("?")[1] ?? "");
    expect(query.get("media_type")).toBe("application/pdf");
    expect(query.get("kind")).toBe("datasheet");
    expect(query.get("role")).toBe("datasheet");
    expect(query.get("part_id")).toBe(String(PART_ID));
    expect(query.get("filename")).toBe("new-sheet.pdf");
    expect(query.get("is_primary")).toBe("true");

    // The panel reloaded and now shows the uploaded document with a one-tap
    // view button, since the upload was forced primary in its role.
    expect(await screen.findByRole("button", { name: "View datasheet" })).toBeTruthy();
    expect(await screen.findByText("new-sheet.pdf")).toBeTruthy();
  });
});
