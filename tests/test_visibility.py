from __future__ import annotations

import unittest

from wolfkill.models import EventVisibility, Phase, Role, SeerInspection
from wolfkill.presets import create_state_from_role_map
from wolfkill.visibility import VisibilityCompiler


class VisibilityTests(unittest.TestCase):
    def test_private_views_respect_hidden_information(self) -> None:
        state = create_state_from_role_map("classic-6", 7, {"p1": Role.WOLF, "p2": Role.WOLF, "p3": Role.SEER, "p4": Role.WITCH, "p5": Role.VILLAGER, "p6": Role.VILLAGER})
        state.phase = Phase.NIGHT
        state.seer_results["p3"] = [SeerInspection(day=1, target="p1", result="WOLF")]
        state.add_event(visibility=EventVisibility.PRIVATE, channel="wolf", text="secret wolf chat", speaker="p1", recipients=("p1", "p2"))
        compiler = VisibilityCompiler()

        wolf_view = compiler.private_view(state, "p1")
        seer_view = compiler.private_view(state, "p3")
        villager_view = compiler.private_view(state, "p6")

        self.assertEqual([item["seat"] for item in wolf_view["teammates"]], ["p2"])
        self.assertEqual(seer_view["seer_results"][0]["target"], "p1")
        self.assertEqual(villager_view["seer_results"], [])
        self.assertIn("secret wolf chat", [item["text"] for item in wolf_view["all_visible_events"]])
        self.assertNotIn("secret wolf chat", [item["text"] for item in villager_view["all_visible_events"]])

    def test_full_context_uses_recent_window_without_duplicate_public_events(self) -> None:
        state = create_state_from_role_map("classic-6", 7, {"p1": Role.WOLF, "p2": Role.WOLF, "p3": Role.SEER, "p4": Role.WITCH, "p5": Role.VILLAGER, "p6": Role.VILLAGER})
        state.phase = Phase.DAY_SPEECH
        for idx in range(10):
            state.add_event(visibility=EventVisibility.PUBLIC, channel="system", text=f"public-{idx}")
        compiler = VisibilityCompiler(full_context_event_limit=4)

        public_state = compiler.public_state(state)
        private_view = compiler.private_view(state, "p6")

        self.assertNotIn("all_public_events", public_state)
        self.assertEqual(public_state["public_event_count"], 10)
        self.assertEqual(public_state["omitted_public_event_count"], 6)
        self.assertEqual(len(private_view["all_visible_events"]), 4)
        self.assertEqual(private_view["omitted_visible_event_count"], 6)
        self.assertEqual(private_view["all_visible_events"][0]["text"], "public-6")
    def test_public_state_does_not_expose_dead_role(self) -> None:
        state = create_state_from_role_map(
            "classic-6",
            7,
            {"p1": Role.WOLF, "p2": Role.WOLF, "p3": Role.SEER, "p4": Role.WITCH, "p5": Role.VILLAGER, "p6": Role.VILLAGER},
        )
        state.players["p3"].alive = False
        state.players["p3"].death_day = 1
        compiler = VisibilityCompiler()
        public_state = compiler.public_state(state)
        self.assertEqual(public_state["dead_players"][0]["seat"], "p3")
        self.assertNotIn("role", public_state["dead_players"][0])

    def test_private_view_role_specific_fields_matrix(self) -> None:
        state = create_state_from_role_map(
            "classic-6",
            7,
            {"p1": Role.WOLF, "p2": Role.WOLF, "p3": Role.SEER, "p4": Role.WITCH, "p5": Role.VILLAGER, "p6": Role.VILLAGER},
        )
        state.phase = Phase.WITCH_ACTION
        state.current_night.wolf_target = 'p5'
        state.seer_results['p3'] = [SeerInspection(day=1, target='p1', result='WOLF')]
        compiler = VisibilityCompiler()

        matrix = {
            'p1': {'teammates': True, 'seer_results': False, 'witch_resources': False, 'night_hint': False},
            'p3': {'teammates': False, 'seer_results': True, 'witch_resources': False, 'night_hint': False},
            'p4': {'teammates': False, 'seer_results': False, 'witch_resources': True, 'night_hint': True},
            'p5': {'teammates': False, 'seer_results': False, 'witch_resources': False, 'night_hint': False},
        }
        for seat, expected in matrix.items():
            view = compiler.private_view(state, seat)
            self.assertEqual(bool(view.get('teammates')), expected['teammates'], seat)
            self.assertEqual(bool(view.get('seer_results')), expected['seer_results'], seat)
            self.assertEqual(bool(view.get('witch_resources')), expected['witch_resources'], seat)
            self.assertEqual(bool(view.get('night_hint')), expected['night_hint'], seat)

    def test_public_state_dead_players_do_not_expose_role_or_death_cause(self) -> None:
        state = create_state_from_role_map(
            "classic-6",
            7,
            {"p1": Role.WOLF, "p2": Role.WOLF, "p3": Role.SEER, "p4": Role.WITCH, "p5": Role.VILLAGER, "p6": Role.VILLAGER},
        )
        state.players['p3'].alive = False
        state.players['p3'].death_day = 1
        state.players['p3'].death_causes = []
        compiler = VisibilityCompiler()
        public_state = compiler.public_state(state)
        dead_player = public_state['dead_players'][0]
        self.assertNotIn('role', dead_player)
        self.assertNotIn('death_causes', dead_player)

    def test_private_event_visibility_matrix_across_roles(self) -> None:
        state = create_state_from_role_map(
            "classic-6",
            7,
            {"p1": Role.WOLF, "p2": Role.WOLF, "p3": Role.SEER, "p4": Role.WITCH, "p5": Role.VILLAGER, "p6": Role.VILLAGER},
        )
        state.phase = Phase.WOLF_CHAT
        state.add_event(
            visibility=EventVisibility.PRIVATE,
            channel="wolf",
            text="wolf-only chat",
            speaker="p1",
            recipients=("p1", "p2"),
        )
        state.phase = Phase.SEER_ACTION
        state.add_event(
            visibility=EventVisibility.PRIVATE,
            channel="system",
            text="seer-only result",
            recipients=("p3",),
        )
        state.phase = Phase.WITCH_ACTION
        state.add_event(
            visibility=EventVisibility.PRIVATE,
            channel="system",
            text="witch-only action",
            recipients=("p4",),
        )
        compiler = VisibilityCompiler()
        expected = {
            "p1": {"wolf-only chat"},
            "p2": {"wolf-only chat"},
            "p3": {"seer-only result"},
            "p4": {"witch-only action"},
            "p5": set(),
            "p6": set(),
        }
        for seat, expected_texts in expected.items():
            view = compiler.private_view(state, seat)
            visible_texts = {item["text"] for item in view["all_visible_events"] if item["visibility"] == "PRIVATE"}
            self.assertEqual(visible_texts, expected_texts, seat)

    def test_incremental_private_events_remain_role_scoped(self) -> None:
        state = create_state_from_role_map(
            "classic-6",
            7,
            {"p1": Role.WOLF, "p2": Role.WOLF, "p3": Role.SEER, "p4": Role.WITCH, "p5": Role.VILLAGER, "p6": Role.VILLAGER},
        )
        state.phase = Phase.WOLF_CHAT
        old_wolf = state.add_event(
            visibility=EventVisibility.PRIVATE,
            channel="wolf",
            text="old wolf chat",
            speaker="p1",
            recipients=("p1", "p2"),
        )
        compiler = VisibilityCompiler()
        wolf_incremental_before = compiler.private_view(state, "p1", since_event_id=old_wolf.index)
        self.assertEqual(wolf_incremental_before["new_visible_events"], [])
        state.phase = Phase.WOLF_ACTION
        state.add_event(
            visibility=EventVisibility.PRIVATE,
            channel="wolf",
            text="new wolf chat",
            speaker="p2",
            recipients=("p1", "p2"),
        )
        state.phase = Phase.SEER_ACTION
        state.add_event(
            visibility=EventVisibility.PRIVATE,
            channel="system",
            text="new seer result",
            recipients=("p3",),
        )
        wolf_incremental = compiler.private_view(state, "p1", since_event_id=old_wolf.index)
        seer_incremental = compiler.private_view(state, "p3", since_event_id=old_wolf.index)
        villager_incremental = compiler.private_view(state, "p5", since_event_id=old_wolf.index)
        self.assertEqual([item["text"] for item in wolf_incremental["new_visible_events"]], ["new wolf chat"])
        self.assertEqual([item["text"] for item in seer_incremental["new_visible_events"]], ["new seer result"])
        self.assertEqual(villager_incremental["new_visible_events"], [])



if __name__ == "__main__":
    unittest.main()
