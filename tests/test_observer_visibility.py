from __future__ import annotations

import unittest

from wolfkill.models import EventVisibility, Phase, Role
from wolfkill.observer_visibility import ObserverVisibilityPolicy
from wolfkill.presets import create_state_from_role_map


class _FakeEvent:
    def __init__(self, *, recipients=(), visibility=EventVisibility.PRIVATE):
        self.recipients = recipients
        self.visibility = visibility

    def visible_to(self, seat: str) -> bool:
        if self.visibility == EventVisibility.PUBLIC:
            return True
        return seat in self.recipients


class ObserverVisibilityPolicyTests(unittest.TestCase):
    def _state(self):
        return create_state_from_role_map(
            "classic-6",
            7,
            {"p1": Role.WOLF, "p2": Role.WOLF, "p3": Role.SEER, "p4": Role.WITCH, "p5": Role.VILLAGER, "p6": Role.VILLAGER},
        )

    def test_can_observer_receive_event_matrix(self) -> None:
        private_event = _FakeEvent(recipients=("p1", "p2"), visibility=EventVisibility.PRIVATE)
        public_event = _FakeEvent(visibility=EventVisibility.PUBLIC)
        matrix = [
            (None, False, private_event, False),
            ("p5", False, private_event, False),
            ("p1", False, private_event, True),
            ("p5", False, public_event, True),
            ("p5", True, private_event, True),
        ]
        for observer_seat, god_view_active, event, expected in matrix:
            policy = ObserverVisibilityPolicy(observer_seat=observer_seat, god_view_active=god_view_active)
            self.assertEqual(policy.can_observer_receive_event(event), expected)

    def test_should_hide_request_details_matrix(self) -> None:
        state = self._state()
        matrix = [
            (None, False, "p2", Phase.WOLF_CHAT.value, False),
            ("p5", False, "p2", Phase.WOLF_CHAT.value, True),
            ("p1", False, "p2", Phase.WOLF_CHAT.value, False),
            ("p3", False, "p4", Phase.WITCH_ACTION.value, True),
            ("p4", False, "p4", Phase.WITCH_ACTION.value, False),
            ("p5", False, "p2", Phase.DAY_SPEECH.value, False),
            ("p5", False, "p2", Phase.DAY_VOTE.value, False),
            ("p5", True, "p2", Phase.WOLF_ACTION.value, False),
        ]
        for observer_seat, god_view_active, actor_seat, phase, expected in matrix:
            policy = ObserverVisibilityPolicy(observer_seat=observer_seat, god_view_active=god_view_active)
            request = {"phase": phase}
            self.assertEqual(policy.should_hide_request_details(state, actor_seat, request), expected)

    def test_should_pause_for_request_matrix(self) -> None:
        matrix = [
            (None, False, "p1", False),
            ("p1", False, "p1", False),
            ("p1", True, "p1", True),
            ("p1", False, "p2", True),
        ]
        for observer_seat, god_view_active, actor_seat, expected in matrix:
            policy = ObserverVisibilityPolicy(observer_seat=observer_seat, god_view_active=god_view_active)
            self.assertEqual(policy.should_pause_for_request(actor_seat), expected)


if __name__ == "__main__":
    unittest.main()
