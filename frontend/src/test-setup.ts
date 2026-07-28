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
