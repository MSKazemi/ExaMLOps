"""Read a Compose file the way the guards need it: as YAML, with Compose's merge tags understood.

Compose files may use the `!override` and `!reset` tags (Compose 2.24+) to replace or clear a value
a base file set when files are merged. A plain ``yaml.SafeLoader`` rejects any tag it does not know,
so a guard that parses every ``docker-compose*.yml`` fails on the first file that uses one. Here a
tag keeps its value: to a guard reading one file, ``ports: !override [...]`` *is* that list. How
Compose merges it with the base is Compose's job, not what these guards check.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml


class ComposeLoader(yaml.SafeLoader):
    """A SafeLoader that reads `!override` / `!reset` as the value they carry."""


def _keep_value(loader: yaml.SafeLoader, node: yaml.Node) -> Any:
    if isinstance(node, yaml.SequenceNode):
        return loader.construct_sequence(node)
    if isinstance(node, yaml.MappingNode):
        return loader.construct_mapping(node)
    return loader.construct_scalar(node)  # `!reset ""` / `!reset null`


for _tag in ("!override", "!reset"):
    ComposeLoader.add_constructor(_tag, _keep_value)


def load_compose(source: Path | str) -> Any:
    """The parsed document of a Compose file (a path) or of Compose text (a string)."""
    text = source.read_text() if isinstance(source, Path) else source
    return yaml.load(text, Loader=ComposeLoader)  # noqa: S506 - a SafeLoader subclass
