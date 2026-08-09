from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping, Protocol


@dataclass(frozen=True)
class DiscoveredItem:
    url: str
    title: str = ""
    published_at: str = ""
    metadata: Mapping[str, object] = field(default_factory=dict)


class SourceAdapter(Protocol):
    def discover(self, source: Mapping[str, object], cancel=None) -> list[DiscoveredItem]: ...
