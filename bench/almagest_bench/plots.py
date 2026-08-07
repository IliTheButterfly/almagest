"""Drawing the result, and refusing to draw one that would mislead.

## Why this is a matrix and not a bar chart

The form question comes before the colour question, and for this data the answer
is **not a percentage of anything**. With a handful of cases, "50% correct" and
"one of two" are the same fact, and only one of them invites a reader to compare
it against a number from a different run. So every case is drawn individually,
as itself, and the reader counts.

`refuse_rate_chart` exists to make that a rule rather than a habit: below
`MIN_CASES_FOR_RATES` this module will not produce a chart whose y-axis is a
proportion. When the corpus is large enough, the accuracy-versus-latency scatter
in the plan becomes the headline and this matrix becomes the drill-down.

## Colour

Status palette (good / serious / critical), never the categorical slots -- these
cells are states, not series. Status colour never carries meaning alone here:
each cell has a glyph and a word inside it, which is the documented mitigation
and also what makes the picture survive being printed or screenshotted in grey.

The three outcomes are three *states*, deliberately not two:

* **correct** -- good.
* **distractor** -- serious. The model read the label correctly and picked the
  wrong string off it. A prompt problem.
* **misread** -- warning. Within a couple of characters of the truth: a
  transcription slip, repaired by the barcode anchor or a better photograph.
* **fabricated** -- critical. A part number nowhere on the label and not a
  near-miss of the truth. This is the failure the never-auto-accept rule exists
  for, and it must not average in with the three above.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import FancyBboxPatch

#: Below this, no chart in this module may plot a rate. See the module docstring.
MIN_CASES_FOR_RATES = 30

#: Status palette, fixed and never themed.
GOOD = "#0ca30c"
WARNING = "#fab219"
SERIOUS = "#ec835a"
CRITICAL = "#d03b3b"

#: Dark surface and inks, so the picture matches the app it came from.
SURFACE = "#1a1a19"
INK = "#ffffff"
INK_MUTED = "#c3c2b7"
GRID = "#3a3a38"

OUTCOME_STYLE = {
    "correct": (GOOD, "✓", "correct"),
    "misread": (WARNING, "~", "misread"),
    "distractor": (SERIOUS, "!", "distractor"),
    "fabricated": (CRITICAL, "✕", "fabricated"),
    "none": (WARNING, "-", "no answer"),
    "error": (WARNING, "!", "error"),
}


class RefusedToPlot(RuntimeError):
    """This picture would read as evidence it is not. Raised, never warned."""


def refuse_rate_chart(cases: int) -> None:
    if cases < MIN_CASES_FOR_RATES:
        raise RefusedToPlot(
            f"{cases} cases cannot support a rate. A proportion drawn over this many "
            "reads as a measurement and invites comparison with runs it cannot be "
            "compared to. Draw the cases individually and let the reader count."
        )


@dataclass(frozen=True)
class Run:
    """One model's answer on one case, once."""

    case_id: str
    model_id: str
    outcome: str
    proposed: str
    confidence: float | None
    latency_ms: int | None
    prompt_tokens: int | None


@dataclass(frozen=True)
class Cell:
    """Every run of one model on one case.

    A list rather than a single outcome, and that is the whole reason this was
    rewritten: on the ambiguous case the same model at temperature 0 answered
    once and exhausted its reasoning budget the next time. Collapsing repeats to
    a single verdict would have hidden the most useful thing the run found.
    """

    case_id: str
    model_id: str
    runs: list[Run]

    @property
    def outcomes(self) -> list[str]:
        return [run.outcome for run in self.runs]

    @property
    def stable(self) -> bool:
        return len(set(self.outcomes)) <= 1

    @property
    def best(self) -> Run | None:
        """The run a reader should see first: the most favourable outcome.

        Not an average. With repeats this small an average is meaningless, and
        the pairing of "it can do this" with "it did not always" is the finding.
        """
        order = {"correct": 0, "distractor": 1, "fabricated": 2, "none": 3, "error": 4}
        return min(self.runs, key=lambda r: order.get(r.outcome, 9)) if self.runs else None


def outcome_matrix(cells: list[Cell], path: Path, *, title: str, subtitle: str) -> Path:
    """Every case, every model, drawn as itself.

    One row per case, one column per model. The cell carries the glyph, the
    outcome word and what the model actually said -- because on a corpus this
    size the individual answers *are* the result, and a reader who cannot see
    `MCQ-XBEE3` sitting where `XB3-24Z8UM` should be has not been told the
    interesting thing.
    """
    models = sorted({c.model_id for c in cells})
    cases = sorted({c.case_id for c in cells})
    by_key = {(c.case_id, c.model_id): c for c in cells}

    row_h = 1.15
    fig_h = 3.0 + row_h * len(cases)
    fig, ax = plt.subplots(figsize=(3.6 + 3.2 * len(models), fig_h))
    fig.patch.set_facecolor(SURFACE)
    ax.set_facecolor(SURFACE)

    ax.set_xlim(0, len(models))
    ax.set_ylim(0, len(cases))
    ax.set_xticks([])
    ax.set_yticks([])
    for spine in ax.spines.values():
        spine.set_visible(False)

    for column, model in enumerate(models):
        ax.text(
            column + 0.5,
            len(cases) + 0.12,
            model,
            ha="center",
            va="bottom",
            color=INK,
            fontsize=12,
            fontweight="bold",
        )

    for row, case_id in enumerate(cases):
        y = len(cases) - row - 1
        ax.text(
            -0.04,
            y + row_h / 2 - 0.05,
            case_id,
            ha="right",
            va="center",
            color=INK_MUTED,
            fontsize=10,
            family="monospace",
            transform=ax.get_yaxis_transform(which="grid"),
            clip_on=False,
        )
        for column, model in enumerate(models):
            cell = by_key.get((case_id, model))
            best = cell.best if cell else None
            outcome = best.outcome if best else "none"
            colour, glyph, word = OUTCOME_STYLE.get(outcome, OUTCOME_STYLE["none"])

            # 2px surface gap between fills, and rounded ends -- the mark spec.
            for width, edge_colour, face in ((0, "none", colour), (1.6, colour, "none")):
                ax.add_patch(
                    FancyBboxPatch(
                        (column + 0.03, y + 0.08),
                        0.94,
                        0.84,
                        boxstyle="round,pad=0,rounding_size=0.06",
                        linewidth=width,
                        edgecolor=edge_colour,
                        facecolor=face,
                        alpha=0.20 if width == 0 else 1.0,
                    )
                )

            # Glyph + word: status colour never carries the meaning alone.
            ax.text(
                column + 0.09,
                y + 0.68,
                glyph,
                ha="left",
                va="center",
                color=colour,
                fontsize=15,
                fontweight="bold",
            )
            ax.text(
                column + 0.20,
                y + 0.68,
                word,
                ha="left",
                va="center",
                color=INK,
                fontsize=11,
                fontweight="bold",
            )

            if cell is None or best is None:
                continue

            # One pip per run, so an unstable cell shows it. This is the finding
            # the single-verdict version would have hidden.
            if len(cell.runs) > 1:
                for i, run_outcome in enumerate(cell.outcomes):
                    pip, _, _ = OUTCOME_STYLE.get(run_outcome, OUTCOME_STYLE["none"])
                    ax.plot(
                        [column + 0.90 - i * 0.075],
                        [y + 0.68],
                        marker="o",
                        markersize=8,
                        color=pip,
                        markeredgewidth=0,
                    )
                if not cell.stable:
                    ax.text(
                        column + 0.90,
                        y + 0.50,
                        f"{cell.outcomes.count(outcome)} of {len(cell.runs)} runs",
                        ha="right",
                        va="center",
                        color=WARNING,
                        fontsize=8.5,
                        fontweight="bold",
                    )

            said = best.proposed or "(nothing)"
            ax.text(
                column + 0.09,
                y + 0.42,
                said[:32],
                ha="left",
                va="center",
                color=INK,
                fontsize=10,
                family="monospace",
            )

            bits = []
            if best.confidence is not None:
                bits.append(f"conf {best.confidence:.2f}")
            if best.latency_ms is not None:
                bits.append(f"{best.latency_ms / 1000:.0f}s")
            if best.prompt_tokens is not None:
                bits.append(f"{best.prompt_tokens} tok")
            ax.text(
                column + 0.09,
                y + 0.22,
                " · ".join(bits),
                ha="left",
                va="center",
                color=INK_MUTED,
                fontsize=9,
            )

    header_in, footer_in = 0.95, 0.55
    top = 1 - header_in / fig_h
    bottom = footer_in / fig_h
    fig.suptitle(
        title,
        color=INK,
        fontsize=15,
        fontweight="bold",
        x=0.02,
        ha="left",
        y=1 - 0.30 / fig_h,
    )
    fig.text(0.02, 1 - 0.62 / fig_h, subtitle, color=INK_MUTED, fontsize=10, ha="left")

    # Two lines. One ran off the right edge of a twelve-row figure, which is the
    # sort of thing only rendering it shows you.
    fig.text(
        0.02,
        0.33 / fig_h,
        "correct = the part number, or nothing when the item carries none"
        "     misread = within two characters of it",
        color=INK_MUTED,
        fontsize=8.5,
        ha="left",
    )
    fig.text(
        0.02,
        0.15 / fig_h,
        "distractor = a different string genuinely printed on the item     fabricated = neither",
        color=INK_MUTED,
        fontsize=8.5,
        ha="left",
    )

    fig.tight_layout(rect=(0.0, bottom, 1.0, top))
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, dpi=170, facecolor=SURFACE)
    plt.close(fig)
    return path
