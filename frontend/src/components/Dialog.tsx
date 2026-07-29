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
 * - **Focus moves in and is trapped.** It lands on the first field rather than on
 *   the close button — the titlebar comes first in the DOM, so the naive "first
 *   focusable" would open every panel with the caret on ✕. Tab and Shift+Tab cycle
 *   inside the panel, and a `focusin` outside it is pulled back, because a *click*
 *   into the page behind is not a Tab and would otherwise leave the trap silently.
 *   The page behind it is `aria-hidden` to nothing because the panel is
 *   `aria-modal`, which is what a screen reader reads to mean the same thing.
 * - **The page behind does not scroll.** The panel's own body does. Without the
 *   lock, a flick anywhere near the backdrop scrolls the container page underneath
 *   and the user loses the place they will be returned to.
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
 * How many panels are open, so the last one out restores the page's scrolling.
 *
 * A counter rather than a boolean because a panel that opens a second panel would
 * otherwise unlock the page when the inner one closes, and re-entrancy bugs in a
 * scroll lock are invisible until somebody is standing at a shelf with a phone.
 */
let openPanels = 0;

/**
 * Whether a candidate can actually take focus.
 *
 * Not `offsetParent !== null`, which is the usual trick: jsdom does not implement
 * `offsetParent` at all and answers `null` for everything, so that test empties the
 * list and quietly disables the focus trap in precisely the environment the trap is
 * asserted in. `display`/`visibility`/`hidden` are answered honestly in both.
 */
function focusable(element: HTMLElement): boolean {
  if (element === element.ownerDocument.activeElement) {
    return true;
  }
  if (element.hasAttribute("hidden") || element.getAttribute("aria-hidden") === "true") {
    return false;
  }
  const style = element.ownerDocument.defaultView?.getComputedStyle(element);
  return style === undefined || (style.display !== "none" && style.visibility !== "hidden");
}

function lockPageScroll(): () => void {
  const { body } = document;
  if (openPanels === 0) {
    body.dataset["scrollLocked"] = body.style.overflow;
    body.style.overflow = "hidden";
  }
  openPanels += 1;
  return () => {
    openPanels -= 1;
    if (openPanels === 0) {
      body.style.overflow = body.dataset["scrollLocked"] ?? "";
      delete body.dataset["scrollLocked"];
    }
  };
}

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
  const body = useRef<HTMLDivElement | null>(null);
  const titleId = useId();
  const noteId = useId();

  const focusables = useCallback((root: HTMLElement | null): HTMLElement[] => {
    if (root === null) {
      return [];
    }
    return Array.from(root.querySelectorAll<HTMLElement>(FOCUSABLE)).filter(focusable);
  }, []);

  /**
   * Where the caret goes when the panel opens.
   *
   * The body before the titlebar, deliberately: `.dialog-head` holds the close
   * button and comes first in the DOM, so "the first focusable in the panel" opens
   * every editing panel with the caret on ✕ and a keyboard user has to Tab past
   * the exit to reach the name field.
   */
  const entryPoint = useCallback((): HTMLElement | null => {
    return focusables(body.current)[0] ?? focusables(panel.current)[0] ?? panel.current;
  }, [focusables]);

  // Mount: lock the page behind, remember who opened this, put the caret inside,
  // keep it inside, and give it back on the way out. All one effect so the
  // containment listener is detached *before* focus is handed back — otherwise it
  // would yank the caret into a panel that is unmounting.
  //
  // The cleanup runs on unmount whatever closed the panel, so a caller that closes
  // itself after a successful save restores focus too.
  useEffect(() => {
    const unlock = lockPageScroll();
    const opener = document.activeElement;
    entryPoint()?.focus();

    function contain(event: FocusEvent): void {
      const root = panel.current;
      const target = event.target;
      if (root === null || !(target instanceof Node) || root.contains(target)) {
        return;
      }
      entryPoint()?.focus();
    }
    document.addEventListener("focusin", contain);

    return () => {
      document.removeEventListener("focusin", contain);
      unlock();
      if (opener instanceof HTMLElement && document.contains(opener)) {
        opener.focus();
      }
    };
  }, [entryPoint]);

  /**
   * Back is a dismissal gesture, so it goes through `onClose` like the other three.
   *
   * On a phone, Back *is* how you close a sheet — and because a panel lives in a
   * query parameter, it used to leave the container page entirely and take an
   * unsent draft with it, which is the one dismissal path the discard guard could
   * not see. While there is unsaved work this owns one extra history entry: the
   * first Back pops that entry instead of the panel's URL, so nothing has navigated
   * yet, and the handler puts the entry back and asks the caller to close. With
   * nothing unsaved no entry is pushed and Back closes the panel by ordinary
   * navigation, which is what a user expects.
   *
   * `window.history` directly rather than the router: the entry is a placeholder at
   * the URL the page is already on, not a location the router should know about.
   */
  const closeRef = useRef(onClose);
  closeRef.current = onClose;
  useEffect(() => {
    if (!unsaved) {
      return;
    }
    const marker = { almagestDialogGuard: true };
    window.history.pushState(marker, "", window.location.href);
    function onPop(): void {
      window.history.pushState(marker, "", window.location.href);
      closeRef.current();
    }
    window.addEventListener("popstate", onPop);
    return () => window.removeEventListener("popstate", onPop);
  }, [unsaved]);

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
      const items = focusables(panel.current);
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
        <div className="dialog-body" ref={body}>
          {children}
        </div>
      </div>
    </div>,
    document.body,
  );
}
