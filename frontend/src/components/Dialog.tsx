/**
 * The one popup panel in the app.
 *
 * Iliana: *"Use pop up panels to edit details like name, description and such."*
 * There was no dialog primitive at all before this — every editing task was a
 * route — so this is deliberately **one** small component that everything reuses,
 * rather than a `position: fixed` div per screen. A second hand-rolled one is how
 * a codebase ends up with two focus traps, one of which is broken.
 *
 * What it owes, and what the tests pin:
 *
 * - **Escape closes it**, and so does a click on the backdrop. Both go through
 *   `onClose`, so a caller holding unsaved work can intercept them in one place
 *   rather than three.
 * - **Focus moves in and is trapped.** Tab and Shift+Tab cycle inside the panel;
 *   the page behind it is `aria-hidden` to nothing because the panel is
 *   `aria-modal`, which is what a screen reader reads to mean the same thing.
 * - **Focus is restored** to whatever opened it. The trigger is a button in a card
 *   somewhere down a long page, and losing the caret to `<body>` means losing your
 *   place entirely on a keyboard or a screen reader.
 * - **Unsaved work says so, in the titlebar.** A panel is easy to walk away from,
 *   so `unsaved` puts a badge where the close button is rather than trusting the
 *   caller to remember. It is a `--warn` badge with a "!" glyph, so the state is
 *   carried by a word and a shape and not only by a hue.
 *
 * Not a `<dialog>` element: `showModal()` gives the trap and the backdrop for
 * free, but its top-layer rendering ignores the theme variables inherited through
 * `.app`, and the iOS Safari versions this has to run on shipped it late enough
 * that the polyfill would be larger than this file.
 */

import { useCallback, useEffect, useId, useRef, useState, type ReactNode } from "react";
import { createPortal } from "react-dom";

import { Notice } from "./Feedback";

const FOCUSABLE = [
  "a[href]",
  "button:not([disabled])",
  "input:not([disabled])",
  "select:not([disabled])",
  "textarea:not([disabled])",
  '[tabindex]:not([tabindex="-1"])',
].join(",");

/**
 * "You have unsaved changes" — asked, once, wherever a panel can be dismissed.
 *
 * A panel over a page is much easier to walk away from than a page was: Escape,
 * the backdrop and the close button are all one gesture. So a panel holding an
 * unsent edit does not close on any of them; it asks, and the two answers are
 * spelled out rather than being Yes and No.
 */
export function useDiscardGuard(
  dirty: boolean,
  close: () => void,
): {
  readonly asking: boolean;
  readonly requestClose: () => void;
  readonly keepEditing: () => void;
  readonly discard: () => void;
} {
  const [asking, setAsking] = useState(false);
  return {
    asking,
    requestClose: () => (dirty ? setAsking(true) : close()),
    keepEditing: () => setAsking(false),
    discard: () => {
      setAsking(false);
      close();
    },
  };
}

export function DiscardPrompt({
  onKeepEditing,
  onDiscard,
  what,
}: {
  onKeepEditing: () => void;
  onDiscard: () => void;
  /** What would be lost, in the user's terms. */
  what: string;
}) {
  return (
    <Notice kind="warn" title="Nothing here has been saved yet">
      <p style={{ margin: 0 }}>Closing now loses {what}.</p>
      <div className="row">
        <button type="button" className="primary" onClick={onKeepEditing}>
          Keep editing
        </button>
        <span className="spacer" />
        <button type="button" className="danger" onClick={onDiscard}>
          Discard the changes
        </button>
      </div>
    </Notice>
  );
}

export interface DialogProps {
  readonly title: string;
  /** Escape, the backdrop and the close button all call this. */
  readonly onClose: () => void;
  /** Draws the "unsaved" badge in the titlebar. Purely a statement of fact —
   * this component never blocks a close; see `ContainerEditMode`, which asks. */
  readonly unsaved?: boolean | undefined;
  /** A sentence under the title saying what this panel does or how it saves. */
  readonly note?: string | undefined;
  readonly children: ReactNode;
}

export function Dialog({ title, onClose, unsaved = false, note, children }: DialogProps) {
  const panel = useRef<HTMLDivElement | null>(null);
  const titleId = useId();
  const noteId = useId();

  const focusables = useCallback((): HTMLElement[] => {
    const root = panel.current;
    if (root === null) {
      return [];
    }
    return Array.from(root.querySelectorAll<HTMLElement>(FOCUSABLE)).filter(
      (element) => element.offsetParent !== null || element === document.activeElement,
    );
  }, []);

  // Mount: remember who opened this, put the caret inside, and give it back on
  // the way out. The cleanup runs on unmount whatever closed the panel, so a
  // caller that closes itself after a successful save restores focus too.
  useEffect(() => {
    const opener = document.activeElement;
    const first = focusables()[0] ?? panel.current;
    first?.focus();
    return () => {
      if (opener instanceof HTMLElement && document.contains(opener)) {
        opener.focus();
      }
    };
  }, [focusables]);

  useEffect(() => {
    function onKeyDown(event: KeyboardEvent): void {
      if (event.key === "Escape") {
        event.stopPropagation();
        onClose();
        return;
      }
      if (event.key !== "Tab") {
        return;
      }
      const items = focusables();
      if (items.length === 0) {
        return;
      }
      const first = items[0] as HTMLElement;
      const last = items[items.length - 1] as HTMLElement;
      const active = document.activeElement;
      // Wrapping by hand rather than by `inert` on the rest of the page: `inert`
      // is newer than the browsers this targets, and a trap that only works on
      // Chrome is not a trap.
      if (!event.shiftKey && (active === last || !panel.current?.contains(active))) {
        event.preventDefault();
        first.focus();
      } else if (event.shiftKey && (active === first || !panel.current?.contains(active))) {
        event.preventDefault();
        last.focus();
      }
    }
    document.addEventListener("keydown", onKeyDown, true);
    return () => document.removeEventListener("keydown", onKeyDown, true);
  }, [focusables, onClose]);

  return createPortal(
    <div
      className="dialog-backdrop"
      // A click that started on the backdrop and ended on the backdrop — so a
      // drag that began inside the panel and slipped out does not discard it.
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) {
          onClose();
        }
      }}
    >
      <div
        className="dialog-panel"
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        aria-describedby={note === undefined ? undefined : noteId}
        ref={panel}
        tabIndex={-1}
      >
        <div className="dialog-head">
          <h2 id={titleId} style={{ flex: 1, margin: 0 }}>
            {title}
          </h2>
          {unsaved && <span className="badge badge-warn">unsaved</span>}
          <button type="button" className="dialog-close" onClick={onClose} aria-label="Close">
            ✕
          </button>
        </div>
        {note !== undefined && (
          <p className="muted-note" id={noteId} style={{ margin: 0 }}>
            {note}
          </p>
        )}
        <div className="dialog-body">{children}</div>
      </div>
    </div>,
    document.body,
  );
}
