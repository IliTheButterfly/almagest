"""Almagest over the Model Context Protocol: the inventory as tools an agent calls.

**What this is for.** The questions this system exists to answer — "do I have a
100 nF 0603 X7R", "where is it", "can I build this board", "what is short" — are
exactly the questions asked in the middle of doing something else, in a chat
window, away from the bench. This server puts them one tool call away without a
model having to learn 131 HTTP routes or invent a query language.

**What it is not.** It is not a second API and not a second writer. Every tool
here is a call to the same `/api/...` route the PWA and the station call, so
`app/services/ledger.py` stays the sole writer, server-side idempotency applies
unchanged, and no rule in `CLAUDE.md` gets a second implementation to drift from.
Nothing in this package imports `app.models`, opens the SQLite file, or knows
what a `parameter_value` row looks like.

**The curation is the product.** 131 operations exist; this exposes 25 of them. A
model handed 131 tools chooses badly among them, and most of those routes are for a
human holding a phone in front of a drawer (a provisioning walk, a tag bind) or for
another machine (the extraction worker's claim/submit door) — an agent calling them
is a bug, not a feature. So every operation gets an
explicit disposition in `coverage.py`, and `tests/test_coverage_manifest.py` fails
when a route is added, renamed or removed without one. That is what keeps this
from quietly rotting into a stale fraction of an API nobody remembers to extend:
**you cannot add a route to the backend without this package going red.**

Read `coverage.py` before adding a tool. It is the map.
"""

from __future__ import annotations

__version__ = "0.1.0"

__all__ = ["__version__"]
