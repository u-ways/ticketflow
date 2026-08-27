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
    """Vendor-neutral tracker interface (spec §7.1; widened for plan
    emission by the ADR-0002 revision alongside ADR-0014)."""

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

    def create_item(
        self,
        title: str,
        body: str,
        labels: tuple[str, ...] = (),
        parent_key: str | None = None,
    ) -> str:
        """Create a tracker item; returns its tracker-native external key.

        Plan emission only (ADR-0014). Translation, no idempotency logic:
        the caller owns the emission ledger and the adoption sweep.
        ``parent_key`` is a cosmetic hierarchy mirror (GitHub sub-issues),
        best-effort like every mirror; backends without one ignore it.
        """
        ...

    def update_body(self, external_key: str, body: str) -> None:
        """Replace the item's body. Emission phase 2 writes ``depends-on:``
        lines only after every item's key exists (items before edges)."""
        ...

    def mirror_dependencies(self, external_key: str, depends_on: tuple[str, ...]) -> None:
        """Write the backend's native dependency mirror (ADR-0007).

        Write-only and cosmetic: Jira ``is blocked by`` links, GitHub
        relationships or a board-field convention. Never read as truth; a
        backend with nothing to mirror onto may no-op.
        """
        ...

    def capabilities(self) -> TrackerCapabilities: ...
