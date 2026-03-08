from __future__ import annotations

import io
import unittest
from contextlib import redirect_stdout

from wolfkill.engine import GameEngine, resolve_vote
from wolfkill.gateway import ParticipantGateway
from wolfkill.models import ActionType, Audience, DeathCause, Decision, EventVisibility, Phase, PlayerState, Role
from wolfkill.participants import HumanCliParticipant, KimiCliParticipant, MockParticipant
from wolfkill.presets import create_state_from_preset, create_state_from_role_map, get_preset
from wolfkill.visibility import VisibilityCompiler


class EngineTests(unittest.TestCase):
    def test_all_wolf4_preset_is_four_wolves(self) -> None:
        preset = get_preset("all-wolf-4")
        self.assertEqual(preset.seat_order, ("p1", "p2", "p3", "p4"))
        self.assertEqual([role.value for role in preset.roles], [Role.WOLF.value, Role.WOLF.value, Role.WOLF.value, Role.WOLF.value])

    def test_duel2_preset_is_two_wolves(self) -> None:
        preset = get_preset("duel-2")
        self.assertEqual(preset.seat_order, ("p1", "p2"))
        self.assertEqual([role.value for role in preset.roles], [Role.WOLF.value, Role.WOLF.value])

    def test_classic6_preset_matches_mvp_roles(self) -> None:
        preset = get_preset("classic-6")
        self.assertEqual(preset.seat_order, ("p1", "p2", "p3", "p4", "p5", "p6"))
        self.assertEqual(sorted(role.value for role in preset.roles), sorted([Role.WOLF.value, Role.WOLF.value, Role.SEER.value, Role.WITCH.value, Role.VILLAGER.value, Role.VILLAGER.value]))

    def test_wolves_do_not_win_by_parity_in_mvp_rules(self) -> None:
        from wolfkill.engine import evaluate_winner
        state = create_state_from_role_map("classic-6", 7, {"p1": Role.WOLF, "p2": Role.WOLF, "p3": Role.SEER, "p4": Role.WITCH, "p5": Role.VILLAGER, "p6": Role.VILLAGER})
        state.players["p4"].alive = False
        state.players["p5"].alive = False
        self.assertIsNone(evaluate_winner(state))

    def test_intro_win_condition_text_does_not_mention_parity(self) -> None:
        state = create_state_from_role_map("classic-6", 7, {"p1": Role.WOLF, "p2": Role.WOLF, "p3": Role.SEER, "p4": Role.WITCH, "p5": Role.VILLAGER, "p6": Role.VILLAGER})
        gateway = ParticipantGateway({"p1": HumanCliParticipant("你")}, VisibilityCompiler())
        engine = GameEngine(state, gateway, max_days=0)
        stream = io.StringIO()
        with redirect_stdout(stream):
            engine.run()
        output = stream.getvalue()
        self.assertIn("狼人阵营有两种获胜方式", output)
        self.assertNotIn("存活狼人数≥存活好人数", output)

    def test_day_vote_tie_eliminates_nobody(self) -> None:
        result = resolve_vote({"p1": "p3", "p2": "p4", "p3": "p4", "p4": "p3"}, ("p1", "p2", "p3", "p4"))
        self.assertIsNone(result)

    def test_full_mock_game_is_deterministic(self) -> None:
        first = self._run_mock_game(seed=11)
        second = self._run_mock_game(seed=11)
        self.assertEqual(first.winner, second.winner)
        self.assertEqual([event.text for event in first.public_transcript()], [event.text for event in second.public_transcript()])

    def test_engine_switches_human_to_god_view_after_death(self) -> None:
        state = create_state_from_role_map("classic-6", 7, {"p1": Role.VILLAGER, "p2": Role.WOLF, "p3": Role.WOLF, "p4": Role.SEER, "p5": Role.WITCH, "p6": Role.VILLAGER})
        gateway = ParticipantGateway({"p1": HumanCliParticipant("你"), "p2": MockParticipant("Mock p2", seed=9)}, VisibilityCompiler())
        engine = GameEngine(state, gateway)
        stream = io.StringIO()
        with redirect_stdout(stream):
            engine._public("游戏开始，测试直播。")
            engine._apply_deaths({"p1": {DeathCause.WOLF}})
            engine._add_event(visibility=EventVisibility.PRIVATE, channel="wolf", text="今晚先刀6号。", speaker="p2", recipients=("p2", "p3"))
        output = stream.getvalue()
        self.assertIn("【播报】[消息#1][第1天:准备阶段:系统] 游戏开始，测试直播。", output)
        self.assertIn("现在切换为上帝视角", output)
        self.assertIn("【法官记录/私有】[消息#2][第1天:准备阶段:狼人密谈 2号位] 今晚先刀[6]。", output)

    def test_engine_records_wolf_final_target_in_transcript(self) -> None:
        state = create_state_from_role_map("classic-6", 7, {"p1": Role.VILLAGER, "p2": Role.WOLF, "p3": Role.WOLF, "p4": Role.SEER, "p5": Role.WITCH, "p6": Role.VILLAGER})
        participants = {seat: MockParticipant(f"Mock {seat}", seed=7 + idx) for idx, seat in enumerate(state.seat_order)}
        gateway = ParticipantGateway(participants, VisibilityCompiler())
        engine = GameEngine(state, gateway)
        with redirect_stdout(io.StringIO()):
            engine._run_night()
        self.assertTrue(any(event.text.startswith("狼人最终决定击杀") for event in state.transcript))

    def test_witch_options_include_save_and_poison_when_both_available(self) -> None:
        state = create_state_from_role_map("classic-6", 7, {"p1": Role.WOLF, "p2": Role.WOLF, "p3": Role.SEER, "p4": Role.WITCH, "p5": Role.VILLAGER, "p6": Role.VILLAGER})
        gateway = ParticipantGateway({seat: MockParticipant(f"Mock {seat}", seed=7 + idx) for idx, seat in enumerate(state.seat_order)}, VisibilityCompiler())
        engine = GameEngine(state, gateway)
        captured_specs = {}

        def fake_request_action(current_state, seat, specs, prompt):
            if seat == 'p4' and current_state.phase == Phase.WITCH_ACTION:
                captured_specs['specs'] = specs
                return Decision(ActionType.NO_OP)
            first = specs[0]
            if first.requires_target and first.targets:
                return Decision(first.action_type, first.targets[0])
            return Decision(first.action_type)

        # monkeypatch only for this engine instance
        original_request_action = gateway.request_action
        gateway.request_action = fake_request_action  # type: ignore[method-assign]
        try:
            with redirect_stdout(io.StringIO()):
                engine._run_night()
        finally:
            gateway.request_action = original_request_action  # type: ignore[method-assign]

        action_types = [spec.action_type for spec in captured_specs['specs']]
        self.assertIn(ActionType.WITCH_SAVE, action_types)
        self.assertIn(ActionType.WITCH_POISON, action_types)
        self.assertIn(ActionType.NO_OP, action_types)

    def test_wolf_chat_stops_early_when_everyone_says_no_more_discussion(self) -> None:
        class SilentWolf(MockParticipant):
            def speak(self, request):
                return {"text": "无更多讨论"}

        state = create_state_from_role_map("classic-6", 7, {"p1": Role.WOLF, "p2": Role.WOLF, "p3": Role.SEER, "p4": Role.WITCH, "p5": Role.VILLAGER, "p6": Role.VILLAGER})
        participants = {
            "p1": SilentWolf("Wolf p1", seed=7),
            "p2": SilentWolf("Wolf p2", seed=8),
            "p3": MockParticipant("Mock p3", seed=9),
            "p4": MockParticipant("Mock p4", seed=10),
            "p5": MockParticipant("Mock p5", seed=11),
            "p6": MockParticipant("Mock p6", seed=12),
        }
        gateway = ParticipantGateway(participants, VisibilityCompiler())
        engine = GameEngine(state, gateway)

        with redirect_stdout(io.StringIO()):
            engine._run_night()

        wolf_chat_events = [event for event in state.transcript if event.phase == Phase.WOLF_CHAT and event.channel == 'wolf']
        self.assertEqual(len(wolf_chat_events), 2)
        self.assertTrue(all(event.text == '无更多讨论' for event in wolf_chat_events))

    def test_wolf_chat_caps_at_five_rounds_when_discussion_continues(self) -> None:
        class TalkativeWolf(MockParticipant):
            def __init__(self, name: str, seed: int = 0):
                super().__init__(name, seed=seed)
                self.calls = 0

            def speak(self, request):
                self.calls += 1
                return {"text": f"继续讨论{self.calls}"}

        state = create_state_from_role_map("classic-6", 7, {"p1": Role.WOLF, "p2": Role.WOLF, "p3": Role.SEER, "p4": Role.WITCH, "p5": Role.VILLAGER, "p6": Role.VILLAGER})
        wolf1 = TalkativeWolf("Wolf p1", seed=7)
        wolf2 = TalkativeWolf("Wolf p2", seed=8)
        participants = {
            "p1": wolf1,
            "p2": wolf2,
            "p3": MockParticipant("Mock p3", seed=9),
            "p4": MockParticipant("Mock p4", seed=10),
            "p5": MockParticipant("Mock p5", seed=11),
            "p6": MockParticipant("Mock p6", seed=12),
        }
        gateway = ParticipantGateway(participants, VisibilityCompiler())
        engine = GameEngine(state, gateway)

        with redirect_stdout(io.StringIO()):
            engine._run_night()

        wolf_chat_events = [event for event in state.transcript if event.phase == Phase.WOLF_CHAT and event.channel == 'wolf']
        self.assertEqual(len(wolf_chat_events), 10)
        self.assertEqual(wolf1.calls, 5)
        self.assertEqual(wolf2.calls, 5)

    def test_engine_records_witch_action_in_transcript(self) -> None:
        state = create_state_from_role_map("classic-6", 7, {"p1": Role.WITCH, "p2": Role.WOLF, "p3": Role.WOLF, "p4": Role.SEER, "p5": Role.VILLAGER, "p6": Role.VILLAGER})
        participants = {seat: MockParticipant(f"Mock {seat}", seed=7 + idx) for idx, seat in enumerate(state.seat_order)}
        gateway = ParticipantGateway(participants, VisibilityCompiler())
        engine = GameEngine(state, gateway)
        with redirect_stdout(io.StringIO()):
            engine._run_night()
        self.assertTrue(any(event.text.startswith("女巫") for event in state.transcript))

    def test_day_speech_prompt_is_not_forced_brief(self) -> None:
        state = create_state_from_role_map("classic-6", 7, {"p1": Role.VILLAGER, "p2": Role.WOLF, "p3": Role.WOLF, "p4": Role.SEER, "p5": Role.WITCH, "p6": Role.VILLAGER})
        gateway = ParticipantGateway({seat: MockParticipant(f"Mock {seat}", seed=7 + idx) for idx, seat in enumerate(state.seat_order)}, VisibilityCompiler())
        engine = GameEngine(state, gateway, max_days=1)
        prompts: list[str] = []

        def fake_request_speech(current_state, seat, audience, prompt):
            prompts.append(prompt)
            return "测试发言"

        gateway.request_speech = fake_request_speech  # type: ignore[method-assign]
        with redirect_stdout(io.StringIO()):
            engine._run_day()

        self.assertTrue(any("根据当前局势发表白天发言" in prompt for prompt in prompts))
        self.assertFalse(any("简短" in prompt for prompt in prompts))

    def test_kimi_participants_receive_expected_night_contexts(self) -> None:
        import json

        class RecordingKimiParticipant(KimiCliParticipant):
            def __init__(self, *args, scripted_action=None, **kwargs):
                super().__init__(*args, **kwargs)
                self.captured_requests: list[dict] = []
                self._scripted_action = scripted_action

            def _run_prompt(self, mode: str, prompt: str) -> str:
                marker = prompt.rfind("{\n  \"mode\"")
                assert marker != -1
                payload = json.loads(prompt[marker:])
                self.captured_requests.append(payload)
                phase = payload['request']['phase']
                if mode == 'speech':
                    return '{"text":"我来补一句夜间意见。"}'
                if self._scripted_action is not None:
                    return json.dumps(self._scripted_action(payload), ensure_ascii=False)
                if phase == 'WOLF_ACTION':
                    targets = payload['request']['options'][0]['targets']
                    return json.dumps({"action_type":"WOLF_KILL", "target": targets[0]}, ensure_ascii=False)
                if phase == 'WITCH_ACTION':
                    return '{"action_type":"NO_OP","target":null}'
                if phase == 'SEER_ACTION':
                    targets = payload['request']['options'][0]['targets']
                    return json.dumps({"action_type":"SEER_INSPECT", "target": targets[0]}, ensure_ascii=False)
                return '{"action_type":"NO_OP","target":null}'

        state = create_state_from_role_map(
            "classic-6",
            7,
            {
                "p1": Role.WOLF,
                "p2": Role.WOLF,
                "p3": Role.VILLAGER,
                "p4": Role.WITCH,
                "p5": Role.SEER,
                "p6": Role.VILLAGER,
            },
        )
        wolf_a = RecordingKimiParticipant(name="Kimi Wolf A", model="kimi-code/kimi-for-coding")
        wolf_b = RecordingKimiParticipant(name="Kimi Wolf B", model="kimi-code/kimi-for-coding")
        witch = RecordingKimiParticipant(name="Kimi Witch", model="kimi-code/kimi-for-coding")
        participants = {
            "p1": wolf_a,
            "p2": wolf_b,
            "p3": MockParticipant("Mock p3", seed=10),
            "p4": witch,
            "p5": MockParticipant("Mock p5", seed=11),
            "p6": MockParticipant("Mock p6", seed=12),
        }
        gateway = ParticipantGateway(
            participants,
            VisibilityCompiler(),
            learn_history=["赛前复盘A", "赛前复盘B"],
            previous_games=["上一局总结"],
        )
        engine = GameEngine(state, gateway)

        with redirect_stdout(io.StringIO()):
            engine._run_night()

        wolf_chat_requests = [item for item in wolf_a.captured_requests if item['request']['phase'] == 'WOLF_CHAT']
        wolf_action_requests = [item for item in wolf_a.captured_requests if item['request']['phase'] == 'WOLF_ACTION']
        witch_requests = [item for item in witch.captured_requests if item['request']['phase'] == 'WITCH_ACTION']

        self.assertGreaterEqual(len(wolf_chat_requests), 1)
        self.assertEqual(wolf_chat_requests[0]['mode'], 'speech')
        self.assertEqual(wolf_chat_requests[0]['request']['audience'], 'WOLF')
        self.assertEqual(wolf_chat_requests[0]['request']['context_mode'], 'full')
        self.assertIn('strategy_briefing', wolf_chat_requests[0]['request'])
        self.assertIn('all_visible_events', wolf_chat_requests[0]['request']['private_view'])
        self.assertTrue(wolf_chat_requests[0]['request']['private_view']['teammates'])

        self.assertEqual(len(wolf_action_requests), 1)
        self.assertEqual(wolf_action_requests[0]['mode'], 'decision')
        self.assertEqual(wolf_action_requests[0]['request']['context_mode'], 'incremental')
        self.assertIn('new_visible_events', wolf_action_requests[0]['request']['private_view'])
        self.assertNotIn('all_visible_events', wolf_action_requests[0]['request']['private_view'])
        self.assertTrue(any(event['channel'] == 'wolf' for event in wolf_action_requests[0]['request']['private_view']['new_visible_events']))

        self.assertEqual(len(witch_requests), 1)
        self.assertEqual(witch_requests[0]['mode'], 'decision')
        self.assertEqual(witch_requests[0]['request']['context_mode'], 'full')
        self.assertIn('witch_resources', witch_requests[0]['request']['private_view'])
        self.assertIn('night_hint', witch_requests[0]['request']['private_view'])
        self.assertEqual(witch_requests[0]['request']['private_view']['night_hint']['wolf_target'], state.current_night.wolf_target)
        self.assertIn('strategy_briefing', witch_requests[0]['request'])

    def test_announce_game_intro_mentions_strategy_guide_label(self) -> None:
        state = create_state_from_role_map(
            "classic-6",
            7,
            {
                "p1": Role.VILLAGER,
                "p2": Role.WOLF,
                "p3": Role.WOLF,
                "p4": Role.SEER,
                "p5": Role.WITCH,
                "p6": Role.VILLAGER,
            },
        )
        participants = {seat: MockParticipant(name=f"Mock {seat}", seed=7 + index) for index, seat in enumerate(state.seat_order, start=1)}
        gateway = ParticipantGateway(participants, VisibilityCompiler(), learn_history=["策略指南正文"])
        engine = GameEngine(state, gateway, max_days=0, learn_history=["策略指南正文"], learn_briefing_label="strategy_guide.md")

        with redirect_stdout(io.StringIO()):
            engine._announce_game_intro()

        public_system_events = [
            event.text for event in state.transcript
            if event.visibility == EventVisibility.PUBLIC and event.channel == 'system'
        ]
        self.assertIn('【系统】已为 AI 玩家加载赛前策略知识库 strategy_guide.md。', public_system_events)

    def _run_mock_game(self, seed: int):
        preset = get_preset("classic-6")
        participants = {seat: MockParticipant(name=f"Mock {seat}", seed=seed + index) for index, seat in enumerate(preset.seat_order, start=1)}
        state = create_state_from_preset("classic-6", seed, names={seat: adapter.name for seat, adapter in participants.items()})
        gateway = ParticipantGateway(participants, VisibilityCompiler())
        try:
            with redirect_stdout(io.StringIO()):
                return GameEngine(state, gateway).run()
        finally:
            gateway.close()


if __name__ == "__main__":
    unittest.main()
