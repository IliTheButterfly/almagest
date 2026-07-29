/**
 * The one popup panel, and the four things that make a popup usable rather than a
 * trap — asserted here rather than trusted, because every one of them is invisible
 * when it breaks.
 *
 * There is exactly one dialog primitive in this app on purpose (see `Dialog.tsx`),
 * so this file is the only place the keyboard contract is pinned, and every editing
 * panel inherits whatever it says. The four:
 *
 * 1. **Escape closes**, through the same `onClose` the backdrop and ✕ use, so a
 *    caller holding unsaved work intercepts one path and not three.
 * 2. **Focus goes to the first field, not to ✕.** The titlebar precedes the body in
 *    the DOM, so the obvious "focus the first focusable" opens every panel with the
 *    caret on the exit — which is both useless and, on a rename panel, dangerous.
 * 3. **Focus stays in.** Tab and Shift+Tab wrap, and a focus that lands outside —
 *    a *click* into the page behind, which is not a Tab — is pulled back.
 * 4. **Focus is restored to whatever opened it.** The trigger is a button in a card
 *    partway down a long page; losing the caret to `<body>` loses the user's place.
 *
 * Plus the page behind must not scroll while a panel is over it, and unsaved work
 * must say so where the close button is.
 */

import { fireEvent, render, screen } from "@testing-library/react";
import { useState } from "react";
import { expect, it, vi } from "vitest";

import { Dialog, DiscardPrompt, useDiscardGuard } from "./Dialog";

function TwoFields({ onClose, unsaved = false }: { onClose: () => void; unsaved?: boolean }) {
  return (
    <Dialog title="Name and description" onClose={onClose} unsaved={unsaved} note="A note.">
      <label className="field">
        <span>Name</span>
        <input aria-label="Name" />
      </label>
      <button type="button">Save</button>
    </Dialog>
  );
}

it("closes on Escape, through the same handler as the backdrop and the ✕", () => {
  const onClose = vi.fn();
  render(<TwoFields onClose={onClose} />);

  fireEvent.keyDown(document, { key: "Escape" });
  expect(onClose).toHaveBeenCalledTimes(1);

  fireEvent.click(screen.getByRole("button", { name: "Close" }));
  expect(onClose).toHaveBeenCalledTimes(2);
});

it("puts the caret on the first field rather than on the close button", () => {
  render(<TwoFields onClose={() => undefined} />);
  expect(document.activeElement).toBe(screen.getByLabelText("Name"));
});

it("wraps Tab and Shift+Tab inside the panel", () => {
  render(<TwoFields onClose={() => undefined} />);
  const close = screen.getByRole("button", { name: "Close" });
  const save = screen.getByRole("button", { name: "Save" });

  // Forward off the end comes back to the top of the panel — ✕ is first in DOM
  // order even though it is not where the caret started.
  save.focus();
  fireEvent.keyDown(document, { key: "Tab" });
  expect(document.activeElement).toBe(close);

  // And backward off the top goes to the bottom.
  close.focus();
  fireEvent.keyDown(document, { key: "Tab", shiftKey: true });
  expect(document.activeElement).toBe(save);
});

it("pulls focus back when it lands outside the panel", () => {
  render(
    <>
      <button type="button">Behind the panel</button>
      <TwoFields onClose={() => undefined} />
    </>,
  );

  // A click, not a Tab: the key handler never sees this, so containment has to
  // come from `focusin` or the trap leaks silently.
  screen.getByRole("button", { name: "Behind the panel" }).focus();
  expect(document.activeElement).toBe(screen.getByLabelText("Name"));
});

it("gives focus back to whatever opened it", () => {
  function Host() {
    const [open, setOpen] = useState(false);
    return (
      <>
        <button type="button" onClick={() => setOpen(true)}>
          Name and description…
        </button>
        {open && <TwoFields onClose={() => setOpen(false)} />}
      </>
    );
  }
  render(<Host />);
  const trigger = screen.getByRole("button", { name: "Name and description…" });

  trigger.focus();
  fireEvent.click(trigger);
  expect(document.activeElement).toBe(screen.getByLabelText("Name"));

  fireEvent.keyDown(document, { key: "Escape" });
  expect(screen.queryByRole("dialog")).toBeNull();
  expect(document.activeElement).toBe(trigger);
});

it("names itself and its note to a screen reader", () => {
  render(<TwoFields onClose={() => undefined} />);
  const dialog = screen.getByRole("dialog");
  expect(dialog.getAttribute("aria-modal")).toBe("true");
  const labelId = dialog.getAttribute("aria-labelledby");
  expect(labelId).not.toBeNull();
  expect(document.getElementById(labelId as string)?.textContent).toBe("Name and description");
  const noteId = dialog.getAttribute("aria-describedby");
  expect(document.getElementById(noteId as string)?.textContent).toBe("A note.");
});

it("stops the page behind from scrolling, and lets it scroll again after", () => {
  const { unmount } = render(<TwoFields onClose={() => undefined} />);
  expect(document.body.style.overflow).toBe("hidden");
  unmount();
  expect(document.body.style.overflow).toBe("");
});

it("says unsaved in the titlebar, in a word and not only a hue", () => {
  const { rerender } = render(<TwoFields onClose={() => undefined} />);
  expect(screen.queryByText("unsaved")).toBeNull();

  rerender(<TwoFields onClose={() => undefined} unsaved />);
  const badge = screen.getByText("unsaved");
  expect(badge.className).toContain("badge-warn");
});

/**
 * The guard is separate from the panel on purpose: `Dialog` never refuses to close,
 * so a caller with a draft is the one that asks. Both answers are spelled out —
 * "Keep editing" and "Discard the changes" — rather than being Yes and No.
 */
it("asks before discarding an unsent edit, and closes when it is not dirty", () => {
  function Guarded({ dirty }: { dirty: boolean }) {
    const [open, setOpen] = useState(true);
    const guard = useDiscardGuard(dirty, () => setOpen(false));
    if (!open) {
      return <p>closed</p>;
    }
    return (
      <Dialog title="Slots inside" onClose={guard.requestClose} unsaved={dirty}>
        {guard.asking && (
          <DiscardPrompt
            what="the layout you have drawn"
            onKeepEditing={guard.keepEditing}
            onDiscard={guard.discard}
          />
        )}
        <button type="button">Something to focus</button>
      </Dialog>
    );
  }

  const { unmount } = render(<Guarded dirty />);
  fireEvent.keyDown(document, { key: "Escape" });
  expect(screen.getByText(/Nothing here has been saved yet/)).toBeTruthy();
  expect(screen.queryByText("closed")).toBeNull();

  fireEvent.click(screen.getByRole("button", { name: "Keep editing" }));
  expect(screen.getByRole("dialog")).toBeTruthy();

  fireEvent.keyDown(document, { key: "Escape" });
  fireEvent.click(screen.getByRole("button", { name: "Discard the changes" }));
  expect(screen.getByText("closed")).toBeTruthy();
  unmount();

  render(<Guarded dirty={false} />);
  fireEvent.keyDown(document, { key: "Escape" });
  expect(screen.getByText("closed")).toBeTruthy();
});
