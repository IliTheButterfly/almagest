"""The one shape every family decoder returns.

Split out of `__init__` only to keep the family modules from importing their own
package — they need this type, and the registry needs them.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class DecodedPart:
    """What one part number was found to encode. **Never more than that.**

    `parameters` is keyed by `parameter_template.name` and its values are raw
    strings for `services.parameters.set_numeric` / `set_choice`. That is not a
    convenience: every numeric carries its unit *in the string*
    (`"100 nF"`, `"50 V"`, `"10 kohm"`), so it is parsed by the same grammar as
    hand-typed input and lands with `value_min`/`value_max` populated. A decoder
    that returned a bare float would need every caller to remember the unit, and
    a caller that got it wrong would write a plausible number that is silently
    invisible to range search.

    A tolerance is folded into the same string (`"100 nF ±10%"`) because the
    value grammar understands it and `to_interval()` then widens the stored
    interval to the part's real tolerance band — which is what a 100 nF ±10% part
    actually is.

    Partial decodes are the normal case, not a failure: a field whose code is not
    in the manufacturer's published table is simply absent from `parameters` and
    named in `unknown`, so the review queue can show *what* was not understood
    instead of only that something was not.
    """

    #: Registry key of the family that claimed the number, e.g. `murata_grm`.
    family: str

    #: `parameter_template.name` -> raw value string.
    parameters: dict[str, str] = field(default_factory=dict)

    #: Decoded facts with no `parameter_template` to land in — thickness,
    #: packaging, manufacturer. Informational; never written as a parameter.
    extras: dict[str, str] = field(default_factory=dict)

    #: Fields whose position is known but whose code is not in any table we
    #: could verify. Named so a partial decode is legible.
    unknown: tuple[str, ...] = ()

    #: True when the input was decoded as a **printed marking**, not a part
    #: number. A caller in an MPN context must route these to review and never
    #: auto-promote them: `104` is 100 kΩ on a resistor and 100 nF on a
    #: capacitor, and the three digits cannot tell you which one you are holding.
    is_marking: bool = False
