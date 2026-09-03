"""Typed carbon signals — accounting vs decision (ADR 0112 · G9.3–G9.5).

Electricity cannot be traced from source to consumer, so several carbon-intensity metrics
coexist and they are **not interchangeable**. Gorka, Rhodes & Roald (UW–Madison,
arXiv:2411.06560) measured what happens when they are:

    "Disconcertingly, we observe that shifting according to common metrics such as **average
    carbon emissions** can **reduce the amount of emissions allocated to the consumer doing
    the shifting, while increasing the total emissions of the power system**."

That is a greener report and a worse world. It is the fourth and worst instance of this
platform's recurring failure pattern — a well-formed instrument measuring the wrong thing.
The other three manufacture false confidence; this one causes real harm and files a green
report about it.

So every carbon signal here carries a **type**, and the type is enforced rather than
documented:

- **accounting** (attributional / average) — what may be *reported*. This is what the EU
  Energy Efficiency Directive expects, and it is the only correct answer to "how much did we
  emit?".
- **decision** (marginal / consequential) — what may drive *scheduling and placement*. It is
  the only signal that answers "would moving this job reduce total emissions?".

Passing the wrong type **raises**; it does not warn. A warning on a correctness-critical path
is ignored, which is the same conclusion ADR 0111 reached about uncalibrated judges.

**The type is derived from the method, never declared independently.** An operator who could
label an average feed "decision" would reintroduce exactly the error this module exists to
prevent, and the endpoint's number looks identical either way. An unrecognised method yields
``signal_type = "unknown"``, which satisfies **neither** guard — absent beats inferred (P5),
applied where substituting a plausible default is actively harmful.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

__all__ = [
    "ACCOUNTING",
    "DECISION",
    "METHOD_SIGNAL_TYPES",
    "UNKNOWN",
    "CarbonSignal",
    "CarbonSignalTypeError",
    "require_accounting",
    "require_decision",
    "signal_type_for_method",
]

#: Attributional / average — safe to report, unsafe to decide with.
ACCOUNTING = "accounting"
#: Marginal / consequential — safe to decide with, overstates allocated emissions ~2×.
DECISION = "decision"
#: A method nobody has classified. Satisfies neither guard, on purpose.
UNKNOWN = "unknown"

#: The type each known method implies. Adding a method here is a claim about what it measures.
METHOD_SIGNAL_TYPES: dict[str, str] = {
    "average_grid_mix": ACCOUNTING,
    "static_default": ACCOUNTING,
    "operator_supplied": ACCOUNTING,
    "residual_mix": ACCOUNTING,
    "locational_marginal": DECISION,
    "marginal_emissions": DECISION,
    "short_run_marginal": DECISION,
}


class CarbonSignalTypeError(TypeError):
    """A carbon signal was used on a path its type does not permit.

    Raised rather than warned: using an average signal to place a job is the documented way to
    reduce your *allocated* emissions while increasing the system's *total* emissions, and a
    warning on that path would be read past.
    """


def signal_type_for_method(method: str) -> str:
    """The signal type a method implies, or :data:`UNKNOWN` for one nobody has classified."""
    return METHOD_SIGNAL_TYPES.get((method or "").strip().lower(), UNKNOWN)


@dataclass(frozen=True)
class CarbonSignal:
    """One carbon-intensity reading, with what it measures attached to it.

    ``signal_type`` is always derived from ``method`` — see the module docstring for why it is
    not separately settable.
    """

    grams_per_kwh: float
    method: str
    zone: str = ""
    source: str = ""
    fetched_at: str = ""

    @property
    def signal_type(self) -> str:
        return signal_type_for_method(self.method)

    @property
    def is_accounting(self) -> bool:
        return self.signal_type == ACCOUNTING

    @property
    def is_decision(self) -> bool:
        return self.signal_type == DECISION

    def as_dict(self) -> dict[str, Any]:
        """Every carbon figure states its method and its type (ADR 0112 decision 6)."""
        return {
            "grams_per_kwh": self.grams_per_kwh,
            "signal_type": self.signal_type,
            "method": self.method,
            "zone": self.zone,
            "source": self.source,
            "fetched_at": self.fetched_at,
        }


def _reject(signal: CarbonSignal, wanted: str, path: str) -> None:
    raise CarbonSignalTypeError(
        f"{path} requires a '{wanted}' carbon signal, got '{signal.signal_type}' "
        f"(method={signal.method!r}). "
        + (
            "An average/attributional signal reduces the emissions *allocated* to you while "
            "possibly increasing the system's total — see ADR 0112."
            if wanted == DECISION
            else "A marginal signal overstates allocated emissions and would make EED "
            "reporting wrong in the other direction — see ADR 0112."
        )
    )


def require_accounting(signal: CarbonSignal, *, path: str = "emissions reporting") -> CarbonSignal:
    """Return ``signal`` if it may be reported; raise otherwise."""
    if not signal.is_accounting:
        _reject(signal, ACCOUNTING, path)
    return signal


def require_decision(signal: CarbonSignal, *, path: str = "placement") -> CarbonSignal:
    """Return ``signal`` if it may drive a scheduling decision; raise otherwise."""
    if not signal.is_decision:
        _reject(signal, DECISION, path)
    return signal
