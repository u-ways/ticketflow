"""Tracker port (ADR-0002, spec §7.1).

Adapters translate tracker items and human signals into these canonical
shapes. They never decide: no scheduling logic lives behind this interface.
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol

from ticketflow.domain.model import NodeState


@dataclass(frozen=True, slots=True)
class TrackerItem:
    """One tracker item in canonical form."""

    provider: str
    external_key: str
    title: str
    body: str
    etag: str | None = None
    closed: bool = False
    updated_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class TrackerIntent:
    """A human signal observed on the tracker, normalized (ADR-0004).

    ``external_id`` is the idempotency key: re-fetching the same signal must
    produce the same id so ingestion stays idempotent.
    """

    external_id: str
    intent_type: str
    external_key: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class TrackerCapabilities:
    """What this backend can actually do; the core asks rather than assumes."""

    native_dependency_links: bool = False
    custom_state_field: bool = False
    supports_comments: bool = True


class TrackerPort(Protocol):
    """Vendor-neutral tracker interface (spec §7.1)."""

    def fetch_nodes(self, cursor: str | None) -> tuple[list[TrackerItem], str | None]:
        """Canonical items changed since the cursor; returns the next cursor."""
        ...

    def fetch_intents(self, cursor: str | None) -> tuple[list[TrackerIntent], str | None]:
        """Normalized human signals since the cursor; returns the next cursor."""
        ...

    def push_state(self, external_key: str, state: NodeState) -> None:
        """Project a node state onto the tracker item (board projection)."""
        ...

    def push_comment(self, external_key: str, text: str) -> None:
        """Post a comment on the tracker item."""
        ...

    def capabilities(self) -> TrackerCapabilities: ...
