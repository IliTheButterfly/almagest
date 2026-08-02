/**
 * Test-suite defaults, applied before every file.
 *
 * Only one thing lives here, and it is about **reliability, not convenience.**
 *
 * Testing Library's `findBy*` / `waitFor` default to a 1 s budget. That is
 * generous on an idle 12-core workstation and not generous at all when 19 test
 * files run in parallel on the two cores a CI runner actually has — so the suite
 * passes locally and flakes in CI. Observed exactly once, as `LotScreen > shows
 * the lot, its derived path and the balance from the cache` failing to find text
 * that a solo run finds immediately, while the machine was pinned to two cores.
 *
 * A suite that is green at 12 cores and red at 2 is worse than a slow one:
 * merge-when-green stops meaning anything, and the reflex becomes "re-run it",
 * which is how a real regression gets waved through.
 *
 * This raises *patience*, not runtime. A passing assertion returns the moment the
 * DOM settles, so the suite still finishes in about five seconds; only a genuine
 * failure now waits longer before saying so. That trade is the right way round —
 * a slow honest failure beats a fast lying one.
 */

import { configure } from "@testing-library/react";

configure({ asyncUtilTimeout: 5_000 });

/**
 * A `WebSocket` that never connects, unless a test supplies its own.
 *
 * The provisioning walk opens the device bridge on `ws://127.0.0.1:8765`
 * (`lib/tags/useBridge.ts`), and that is right in production: it is how a Flipper
 * on a cable becomes a reader the walk can use. In a test run it is a real
 * connection attempt from every file that renders a walk, and `openBridge` is
 * *designed* to keep retrying with a backoff — so a suite that never asked for a
 * bridge accumulates timers and connection attempts for the life of every such
 * file.
 *
 * That is not hypothetical: it turned `RoomPlanPanel`, a file with no reader in
 * it at all, red under the parallel run while passing alone. Cross-file
 * interference through a shared machine is the worst kind of flake, because the
 * failure names something unrelated to the cause.
 *
 * So the default is inert. A test that wants a bridge stubs `WebSocket` itself
 * (`TagWalkPanel.bridge.test.tsx` does), which also makes the dependency visible
 * in the file that has it rather than ambient everywhere.
 */
class InertWebSocket {
  static readonly CONNECTING = 0;
  static readonly OPEN = 1;
  static readonly CLOSING = 2;
  static readonly CLOSED = 3;
  readyState = InertWebSocket.CONNECTING;
  onopen: unknown = null;
  onclose: unknown = null;
  onerror: unknown = null;
  onmessage: unknown = null;
  constructor(readonly url: string) {}
  send(): void {}
  close(): void {
    this.readyState = InertWebSocket.CLOSED;
  }
  addEventListener(): void {}
  removeEventListener(): void {}
}

globalThis.WebSocket = InertWebSocket as unknown as typeof WebSocket;
