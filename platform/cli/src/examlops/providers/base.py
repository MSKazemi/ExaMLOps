"""Core types for the pluggable calculation-provider substrate.

A **provider** is a swappable calculation strategy behind a stable interface (the Strategy /
Provider pattern). The same substrate serves many *domains* — ``carbon``, ``cost``, later
``drift``/``promotion`` — so the mechanism is written once here and reused (see ``registry``).

A provider can be authored three ways, all behind this one interface:

1. a built-in Python class (``register_provider``),
2. a third-party Python package via the ``exa.providers.<domain>`` entry-point group (a plugin),
3. a declarative YAML/config block whose formula is evaluated safely (``yaml_provider`` +
   ``expression`` — the no-code path for sysadmins).

Everything here is dependency-free and domain-agnostic on purpose.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class ProviderMeta:
    """Self-description of a provider — surfaced by the CLI and dashboard.

    ``methodology`` is human-readable prose; ``uncertainty`` is a fractional ±band (e.g. ``0.30``
    for ±30%); ``outputs`` names the keys ``compute`` returns; ``params`` documents accepted
    input/coefficient names. These let the UI show *which* formula produced a figure and how much
    to trust it, instead of hardcoding a methodology string in the consumer.
    """

    methodology: str = ""
    uncertainty: float | None = None
    units: Mapping[str, str] = field(default_factory=dict)
    outputs: tuple[str, ...] = ()
    params: tuple[str, ...] = ()
    source: str = ""  # citation / URL for the coefficients or model


class Provider(ABC):
    """A named calculation strategy: typed inputs → a dict of named outputs.

    Subclasses implement :meth:`compute`. ``name``/``version`` identify the provider; ``metadata``
    describes it. Implementations must be pure (no I/O) so they stay unit-testable and reusable by
    the CLI, pipeline, and services alike — matching the existing ``finops.carbon`` design.
    """

    name: str = "unnamed"
    version: str = "1.0"

    def metadata(self) -> ProviderMeta:  # noqa: D401 - simple default
        """Describe this provider. Override to advertise methodology/uncertainty/units."""
        return ProviderMeta()

    @abstractmethod
    def compute(self, inputs: Mapping[str, Any]) -> dict[str, Any]:
        """Run the calculation for ``inputs`` and return named outputs."""
        raise NotImplementedError


@dataclass
class ProviderInfo:
    """Discovery record for one provider (mirrors ``cli/_plugins.PluginInfo``).

    ``kind`` is where it came from — ``"builtin"`` | ``"entrypoint"`` | ``"config"``. ``ok`` is
    ``False`` with an ``error`` when a plugin failed to load, so discovery never raises.
    """

    name: str
    kind: str
    ok: bool = True
    provider: Provider | None = None
    error: str | None = None
    value: str = ""  # entry-point target or config source, for display


class ProviderError(Exception):
    """Raised when a requested provider cannot be resolved or built."""


# A factory is anything that produces a Provider — a class, or a callable taking a config mapping.
Factory = Any
