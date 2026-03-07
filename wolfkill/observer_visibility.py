from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ObserverVisibilityPolicy:
    observer_seat: str | None
    god_view_active: bool = False

    def can_observer_receive_event(self, event) -> bool:
        if self.observer_seat is None:
            return False
        if self.god_view_active:
            return True
        return event.visible_to(self.observer_seat)

    def should_hide_request_details(self, state, seat: str, request: dict[str, Any] | None = None) -> bool:
        if self.observer_seat is None or self.god_view_active:
            return False
        observer = state.players.get(self.observer_seat)
        if observer is None:
            return False
        if self.observer_seat == seat:
            return False
        phase = str((request or {}).get("phase") or state.phase.value)
        if phase in {"DAY_SPEECH", "DAY_VOTE"}:
            return False
        if phase in {"WOLF_CHAT", "WOLF_ACTION"} and observer.role.value == "WOLF":
            return False
        return True

    def should_pause_for_request(self, seat: str) -> bool:
        if self.observer_seat is None:
            return False
        if self.observer_seat == seat and not self.god_view_active:
            return False
        return True
