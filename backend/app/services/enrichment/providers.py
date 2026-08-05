"""Where a datasheet URL might come from, cheapest and most falsifiable first.

ADR 0017's cascade. Every provider here answers one question — *what URLs might be
this part's datasheet?* — and **none of them answers it authoritatively**. A
provider proposes; `app.services.datasheet_validation` decides. That split is the
whole design, and it is what lets the last provider in the list be a language
model without the pipeline inheriting a language model's failure mode.

## Ordering is by falsifiability, not by convenience

    1. jlcparts      an offline SQLite dump, exact MPN match, no network
    2. url_pattern   pure string construction from the manufacturer's own scheme
    3. mouser        a free API key, exact MPN lookup
    4. websearch     pages, not answers
    5. model         ranks what the others fetched; never recalls a URL

The first two cost nothing and cannot lie: either the dump has the part or it does
not, either the constructed URL serves a PDF or it 404s. They also cover the bulk
of a passives-heavy inventory, which means **the common case never reaches a model
at all** — and is the fastest case, which is the right way round.

`ManualProvider` sits outside the ordering at priority 0, per `docs/PLAN.md`: a
URL a person supplied wins over everything, and is still validated like everything
else. A human can paste the wrong link too.

## Why a provider returns candidates and not a document

The temptation is to have each provider fetch and return the PDF, so the caller
gets a document or nothing. That would put the network in five places, make every
provider's tests need a transport, and — the real objection — it would let each
provider apply its *own* idea of what counts as the right datasheet. There would
then be five places where the MPN check could be skipped, and the one that skipped
it would be the one that shipped a wrong part.

So the interface returns URLs and the worker owns the single fetch-and-validate
path. Five proposers, one gate.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Protocol

#: A provider's rank in the cascade. Lower runs first and sorts higher in the
#: candidate list, so a `url_pattern` hit outranks a model's suggestion in the UI
#: even when both validate. Stored per candidate rather than recomputed, because
#: which providers exist changes over time and a stored rank keeps an old run's
#: ordering meaningful.
RANK_MANUAL = 0
RANK_JLCPARTS = 1
RANK_URL_PATTERN = 2
RANK_MOUSER = 3
RANK_WEBSEARCH = 4
RANK_MODEL = 5


@dataclass(frozen=True)
class PartQuery:
    """What a provider is told about the part. Exactly what the claim carries.

    `mpn_norm` is the catalogue's normalisation, handed down rather than derived —
    see `datasheet_validation`'s docstring for why a second normaliser is a silent
    failure rather than a loud one.
    """

    mpn: str
    mpn_norm: str
    manufacturer: str | None = None
    name: str = ""


@dataclass(frozen=True)
class Candidate:
    """One proposed URL. Not a result — nothing here has been fetched."""

    url: str
    source: str
    rank: int
    #: Why this provider thinks so. Surfaced in the candidate list beside a
    #: rejection, which is what turns "no datasheet" into a diagnosis.
    note: str | None = None


class DatasheetProvider(Protocol):
    """Propose datasheet URLs for a part. The whole abstraction.

    Implementations must not fetch, must not validate, and must not raise for an
    ordinary miss — an empty tuple is the normal answer for "I do not cover this
    manufacturer", and it is not an error. Raising is reserved for the provider
    itself being broken (a dump that will not open, an API key that is refused),
    which the worker reports as a run failure rather than as a fact about the part.
    """

    name: str
    rank: int

    def propose(self, query: PartQuery) -> Sequence[Candidate]: ...


# ---------------------------------------------------------------------------
# The manufacturer URL-pattern table
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class UrlPattern:
    """One manufacturer's published datasheet URL shape.

    `match` is applied to the **raw** MPN, not the normalised one, because these
    schemes are defined over the manufacturer's own printed part number and its
    internal structure — a series prefix, a dielectric code, a size — which
    normalisation deliberately flattens.
    """

    manufacturer: str
    match: re.Pattern[str]
    #: A `str.format` template over the named groups of `match`, plus `mpn`.
    template: str
    note: str


#: The table. Small on purpose and expected to grow one row at a time as a
#: manufacturer's scheme is confirmed against a real part.
#:
#: **Every row here is a guess until a fetch proves it**, and that is fine — a
#: wrong template produces a 404, which is recorded as `fetch_failed` against this
#: provider and costs nothing. What it must never do is produce a *plausible* URL
#: that serves some other part's datasheet, which is why the patterns are anchored
#: to a series prefix rather than being generic search-URL constructions.
URL_PATTERNS: tuple[UrlPattern, ...] = (
    UrlPattern(
        manufacturer="Murata",
        # GRM/GCM/GRT ceramic capacitors: the series is the first six characters.
        match=re.compile(r"^(?P<series>G[RC][MT]\d{3})", re.IGNORECASE),
        template="https://search.murata.co.jp/Ceramy/image/img/A01X/G101/ENG/{mpn}.pdf",
        note="Murata publishes a per-part PDF under the Ceramy image tree",
    ),
    UrlPattern(
        manufacturer="Yageo",
        match=re.compile(r"^(?P<series>(RC|AC|RT)\d{4})", re.IGNORECASE),
        template="https://www.yageo.com/upload/media/product/productsearch/datasheet/rchip/{series}.pdf",
        note="Yageo publishes one PDF per chip-resistor series, not per part",
    ),
)


class UrlPatternProvider:
    """Construct a datasheet URL from the manufacturer's own numbering scheme.

    Pure string construction — no network, no key, no dump, and therefore no way to
    be *subtly* wrong. It either builds a URL that serves the right PDF or it builds
    one that does not exist, and the validator tells the two apart. That is the
    cheapest useful provider there is, and for a passives-heavy inventory it is
    also the highest-hit-rate one.

    A manufacturer name is used when the part has one, but the pattern's own regex
    is what decides: `GRM188R71H104KA93D` is a Murata part number whether or not
    anybody filled in the manufacturer field, and refusing to act on that would make
    the provider useless for exactly the unfinished stub parts research exists for.
    """

    name = "url_pattern"
    rank = RANK_URL_PATTERN

    def __init__(self, patterns: Sequence[UrlPattern] = URL_PATTERNS) -> None:
        self.patterns = tuple(patterns)

    def propose(self, query: PartQuery) -> Sequence[Candidate]:
        if not query.mpn:
            return ()
        out: list[Candidate] = []
        for pattern in self.patterns:
            found = pattern.match.match(query.mpn)
            if found is None:
                continue
            groups = dict(found.groupdict())
            # Uppercased because every scheme in the table publishes under the
            # printed (upper-case) part number, and a scanned MPN may arrive in any
            # case. Not `normalize_mpn`: that strips the separators these URLs need.
            groups["mpn"] = query.mpn.upper()
            groups = {key: value.upper() for key, value in groups.items() if value is not None}
            out.append(
                Candidate(
                    url=pattern.template.format(**groups),
                    source=self.name,
                    rank=self.rank,
                    note=pattern.note,
                )
            )
        return tuple(out)


class ManualProvider:
    """URLs a person supplied. Priority 0 — wins over everything, per `docs/PLAN.md`.

    Still validated. A human pasting a link is a better source than a model
    guessing one, but "better" is not "exempt": the commonest way to attach the
    wrong datasheet by hand is to paste the family sheet for the neighbouring
    series, which the MPN check catches and a trust rule would not.
    """

    name = "manual"
    rank = RANK_MANUAL

    def __init__(self, urls: dict[str, Sequence[str]] | None = None) -> None:
        #: Keyed by `mpn_norm`, so a hand-supplied URL survives the part number
        #: being re-typed with different spacing.
        self.urls = dict(urls or {})

    def propose(self, query: PartQuery) -> Sequence[Candidate]:
        return tuple(
            Candidate(url=url, source=self.name, rank=self.rank, note="supplied by hand")
            for url in self.urls.get(query.mpn_norm, ())
        )


@dataclass
class FakeProvider:
    """A provider made of a dict, for tests and for the worker's own test suite.

    `docs/PLAN.md`'s rule is that every provider ships a `Fake*Provider` replaying a
    fixture plus one `@pytest.mark.live` contract test skipped by default. This is
    the generic half of that: the network-backed providers each get their own fake
    recorded from a real response, and this one exists so the *cascade* can be
    tested without any of them.
    """

    name: str = "fake"
    rank: int = RANK_WEBSEARCH
    responses: dict[str, Sequence[str]] = field(default_factory=dict)

    def propose(self, query: PartQuery) -> Sequence[Candidate]:
        return tuple(
            Candidate(url=url, source=self.name, rank=self.rank)
            for url in self.responses.get(query.mpn_norm, ())
        )


# ---------------------------------------------------------------------------
# The cascade
# ---------------------------------------------------------------------------


def default_providers() -> tuple[DatasheetProvider, ...]:
    """The cascade as shipped: everything that needs no key, no dump and no model.

    Deliberately short. `jlcparts` wants a downloaded dump, `mouser` an API key and
    `websearch` a SearxNG instance, so each of those is constructed by the worker
    only when its configuration is present — a missing one is a narrower cascade,
    never a failed run. That is the same graceful-degradation shape ADR 0005 gives
    extraction: the fancy half can be absent and the cheap half still delivers.
    """
    return (ManualProvider(), UrlPatternProvider())


def gather(providers: Sequence[DatasheetProvider], query: PartQuery) -> list[Candidate]:
    """Run the cascade and return every proposal, best rank first, deduplicated.

    **Every provider runs.** There is no short-circuit on the first hit, and that is
    deliberate: a part with four candidates from four sources is the case where the
    rejections become diagnostic, and stopping early would throw away exactly the
    evidence that tells a `url_pattern` typo apart from an obscure part. The cost is
    a few extra fetches on parts that were going to resolve anyway, which is cheap
    against the alternative of an undiagnosable `exhausted`.

    Deduplicated by URL, keeping the **best-ranked** proposer — two providers
    naming the same PDF is agreement, and it should be fetched once and attributed
    to the more trustworthy of the two.
    """
    best: dict[str, Candidate] = {}
    for provider in sorted(providers, key=lambda item: item.rank):
        for candidate in provider.propose(query):
            existing = best.get(candidate.url)
            if existing is None or candidate.rank < existing.rank:
                best[candidate.url] = candidate
    return sorted(best.values(), key=lambda item: (item.rank, item.url))
