from __future__ import annotations

import io
import unittest
from unittest.mock import patch
from contextlib import redirect_stdout

from wolfkill import colors
from wolfkill.gateway import ParticipantGateway
from wolfkill.models import ActionSpec, ActionType, Audience, EventVisibility, Phase, Role
from wolfkill.participants import HumanCliParticipant, ParticipantAdapter
from wolfkill.presets import create_state_from_role_map
from wolfkill.visibility import VisibilityCompiler


class BadParticipant(ParticipantAdapter):
    def speak(self, request):
        raise RuntimeError("boom")

    def decide(self, request):
        return {"action_type": "BROKEN", "target": "p9"}


class GoodParticipant(ParticipantAdapter):
    def speak(self, request):
        return {"text": "测试发言"}

    def decide(self, request):
        return {"action_type": "NO_OP", "target": None}


class GatewayTests(unittest.TestCase):
    def _state(self):
        return create_state_from_role_map("classic-6", 7, {"p1": Role.VILLAGER, "p2": Role.WOLF, "p3": Role.WOLF, "p4": Role.SEER, "p5": Role.WITCH, "p6": Role.VILLAGER})

    def test_invalid_action_falls_back_to_no_op(self) -> None:
        state = self._state()
        gateway = ParticipantGateway({"p1": BadParticipant("bad")}, VisibilityCompiler())
        with redirect_stdout(io.StringIO()):
            decision = gateway.request_action(state, "p1", [ActionSpec(ActionType.DAY_VOTE, targets=("p2",), requires_target=True), ActionSpec(ActionType.NO_OP)], "Vote now.")
        self.assertEqual(decision.action_type, ActionType.NO_OP)
        self.assertEqual(len(gateway.issues), 1)

    def test_gateway_prints_waiting_status_for_speech(self) -> None:
        state = self._state()
        state.phase = Phase.DAY_SPEECH
        gateway = ParticipantGateway({"p1": GoodParticipant("good")}, VisibilityCompiler())
        stream = io.StringIO()
        with redirect_stdout(stream):
            text = gateway.request_speech(state, "p1", Audience.PUBLIC, "请发表一句简短的白天发言。")
        output = stream.getvalue()
        self.assertEqual(text, "测试发言")
        self.assertIn("【法官】现在进入第1天白天讨论；请1号位（good）发言。", output)

    def test_gateway_keeps_feed_summary_only_in_records(self) -> None:
        state = self._state()
        state.phase = Phase.DAY_SPEECH
        gateway = ParticipantGateway({"p1": GoodParticipant("good")}, VisibilityCompiler())
        stream = io.StringIO()
        with redirect_stdout(stream):
            gateway.request_speech(state, "p1", Audience.PUBLIC, "请发表一句简短的白天发言。")
        output = stream.getvalue()
        self.assertNotIn("【提示】即将喂给", output)
        self.assertNotIn("喂给 AI", output)
        summary = gateway.timing_summary()
        self.assertEqual(summary["wait_count"], 1)
        self.assertIn("public_events", summary["records"][0])
        self.assertIn("extras", summary["records"][0])

    def test_gateway_hides_private_night_waits_from_living_human(self) -> None:
        state = self._state()
        state.phase = Phase.WOLF_CHAT
        human = HumanCliParticipant("你")
        gateway = ParticipantGateway({"p1": human, "p2": GoodParticipant("good")}, VisibilityCompiler())
        stream = io.StringIO()
        with redirect_stdout(stream):
            text = gateway.request_speech(state, "p2", Audience.WOLF, "请向狼人队友发送一句简短消息。")
        output = stream.getvalue()
        self.assertEqual(text, "测试发言")
        self.assertIn("夜间流程进行中", output)
        self.assertNotIn("狼人请依次交流", output)
        self.assertNotIn("2号位", output)
        self.assertNotIn("good", output)

    def test_gateway_switches_to_god_view_and_replays_hidden_events(self) -> None:
        state = self._state()
        human = HumanCliParticipant("你")
        gateway = ParticipantGateway({"p1": human, "p2": GoodParticipant("good")}, VisibilityCompiler())
        public_event = state.add_event(visibility=EventVisibility.PUBLIC, channel="system", text="这条公开记录已经看过。")
        human.remember_event(public_event.index)
        state.phase = Phase.WOLF_CHAT
        hidden_event = state.add_event(visibility=EventVisibility.PRIVATE, channel="wolf", text="今晚先刀6号。", speaker="p2", recipients=("p2", "p3"))
        state.players["p1"].alive = False
        stream = io.StringIO()
        with redirect_stdout(stream):
            gateway.activate_god_view(state)
        output = stream.getvalue()
        self.assertIn("现在切换为上帝视角", output)
        self.assertIn("【法官记录/私有】[消息#2][第1天:狼人密谈:狼人密谈 2号位] 今晚先刀[6]。", output)
        self.assertEqual(human.last_seen_event_id, hidden_event.index)

    def test_gateway_keeps_seat_colors_after_switching_to_god_view(self) -> None:
        state = self._state()
        human = HumanCliParticipant("你")
        gateway = ParticipantGateway({"p1": human, "p2": GoodParticipant("good")}, VisibilityCompiler())
        state.phase = Phase.WOLF_CHAT
        state.add_event(visibility=EventVisibility.PRIVATE, channel="wolf", text="今晚先刀6号。", speaker="p2", recipients=("p2", "p3"))
        state.players["p1"].alive = False
        stream = io.StringIO()
        original_force = colors._FORCE_COLOR
        colors._FORCE_COLOR = True
        try:
            with redirect_stdout(stream):
                gateway.activate_god_view(state)
        finally:
            colors._FORCE_COLOR = original_force
        self.assertIn("\x1b[36m", stream.getvalue())
    def test_gateway_collects_parallel_votes_concurrently(self) -> None:
        import time

        class SlowVotingParticipant(ParticipantAdapter):
            def speak(self, request):
                return {"text": "slow"}

            def decide(self, request):
                time.sleep(0.05)
                return {"action_type": "DAY_VOTE", "target": "p2"}

        state = self._state()
        state.phase = Phase.DAY_VOTE
        participants = {
            "p1": SlowVotingParticipant("a"),
            "p2": SlowVotingParticipant("b"),
            "p3": SlowVotingParticipant("c"),
        }
        gateway = ParticipantGateway(participants, VisibilityCompiler())
        started_at = time.monotonic()
        with redirect_stdout(io.StringIO()):
            decisions = gateway.request_actions_parallel(
                state,
                [
                    ("p1", [ActionSpec(ActionType.DAY_VOTE, targets=("p2",), requires_target=True)], "vote"),
                    ("p2", [ActionSpec(ActionType.DAY_VOTE, targets=("p1",), requires_target=True)], "vote"),
                    ("p3", [ActionSpec(ActionType.DAY_VOTE, targets=("p1",), requires_target=True)], "vote"),
                ],
            )
        elapsed = time.monotonic() - started_at
        self.assertLess(elapsed, 0.13)
        self.assertEqual(decisions["p1"].action_type, ActionType.DAY_VOTE)
        self.assertEqual(gateway.timing_summary()["longest"]["kind"], "parallel_decision")

    def test_emit_paces_before_printing_message(self) -> None:
        gateway = ParticipantGateway({}, VisibilityCompiler(), narration_delay_seconds=1.0)
        events: list[str] = []

        def fake_pause():
            events.append("pause")

        stream = io.StringIO()
        gateway._pause = fake_pause  # type: ignore[method-assign]
        with redirect_stdout(stream):
            gateway._emit("hello", pace=True)

        self.assertEqual(events, ["pause"])
        self.assertIn("hello", stream.getvalue())

    def test_gateway_hidden_wait_prints_generic_message_once_per_wait(self) -> None:
        state = self._state()
        state.phase = Phase.WOLF_CHAT
        human = HumanCliParticipant("你")
        gateway = ParticipantGateway({"p1": human, "p2": GoodParticipant("good")}, VisibilityCompiler())
        request = gateway._build_base_request(state, "p2", "请向狼人队友发送一句简短消息。")
        request["audience"] = Audience.WOLF.value
        stream = io.StringIO()
        with redirect_stdout(stream):
            gateway._announce_wait(state, "p2", "speech", request)
            gateway._announce_wait(state, "p2", "speech", request)
        output = stream.getvalue()
        self.assertEqual(output.count("等待夜间发言和角色行动"), 1)
        self.assertNotIn("2号位", output)
        self.assertNotIn("good", output)

    def test_gateway_does_not_duplicate_visible_public_speech_result_line(self) -> None:
        state = self._state()
        state.phase = Phase.DAY_SPEECH
        gateway = ParticipantGateway({"p1": GoodParticipant("good")}, VisibilityCompiler())
        stream = io.StringIO()
        with redirect_stdout(stream):
            gateway.request_speech(state, "p1", Audience.PUBLIC, "请根据当前局势发表白天发言，长度和风格由你的阵营策略决定。")
        output = stream.getvalue()
        self.assertNotIn("【结果】（", output)
        self.assertNotIn("发言：测试发言", output)

    def test_gateway_phase_visibility_matrix(self) -> None:
        matrix = [
            (Role.VILLAGER, 'p5', Phase.WOLF_CHAT, 'p2', True),
            (Role.VILLAGER, 'p5', Phase.WOLF_ACTION, 'p2', True),
            (Role.WOLF, 'p1', Phase.WOLF_CHAT, 'p2', False),
            (Role.WOLF, 'p1', Phase.WOLF_ACTION, 'p2', False),
            (Role.SEER, 'p3', Phase.SEER_ACTION, 'p4', True),
            (Role.WITCH, 'p4', Phase.WITCH_ACTION, 'p3', True),
            (Role.VILLAGER, 'p5', Phase.DAY_SPEECH, 'p2', False),
            (Role.VILLAGER, 'p5', Phase.DAY_VOTE, 'p2', False),
        ]
        for observer_role, observer_seat, phase, actor_seat, expected_hidden in matrix:
            state = create_state_from_role_map(
                'classic-6',
                7,
                {'p1': Role.WOLF, 'p2': Role.WOLF, 'p3': Role.SEER, 'p4': Role.WITCH, 'p5': Role.VILLAGER, 'p6': Role.VILLAGER},
            )
            # move observer role if needed
            state.players[observer_seat].role = observer_role
            state.phase = phase
            human = HumanCliParticipant('你')
            participants = {observer_seat: human, actor_seat: GoodParticipant('good')}
            gateway = ParticipantGateway(participants, VisibilityCompiler(), observer_seat=observer_seat)
            request = gateway._build_base_request(state, actor_seat, 'test')
            hidden = gateway._should_hide_request_details(state, actor_seat, request)
            self.assertEqual(hidden, expected_hidden, (observer_role, phase, actor_seat))

    def test_gateway_hidden_wait_prints_generic_message_once_per_night_phase_group(self) -> None:
        state = self._state()
        state.phase = Phase.WOLF_CHAT
        human = HumanCliParticipant("你")
        gateway = ParticipantGateway({"p1": human, "p2": GoodParticipant("good")}, VisibilityCompiler())
        speech_request = gateway._build_base_request(state, "p2", "请向狼人队友发送一句简短消息。")
        speech_request["audience"] = Audience.WOLF.value
        action_request = gateway._build_base_request(state, "p2", "请选择你今晚的狼人行动。")
        action_request["options"] = [{"action_type": "WOLF_KILL", "targets": ["p3"], "requires_target": True, "description": "刀人"}]
        action_request["phase"] = Phase.WOLF_ACTION.value
        stream = io.StringIO()
        with redirect_stdout(stream):
            gateway._announce_wait(state, "p2", "speech", speech_request)
            gateway._announce_done(state, "p2", "speech", elapsed_seconds=1.0)
            gateway._announce_wait(state, "p2", "speech", speech_request)
            gateway._announce_wait(state, "p2", "decision", action_request)
        output = stream.getvalue()
        self.assertEqual(output.count("等待夜间发言和角色行动"), 1)

    def test_human_witch_self_target_note_is_shown(self) -> None:
        import builtins
        state = create_state_from_role_map("classic-6", 7, {"p1": Role.WOLF, "p2": Role.WOLF, "p3": Role.SEER, "p4": Role.WITCH, "p5": Role.VILLAGER, "p6": Role.VILLAGER})
        state.phase = Phase.WITCH_ACTION
        state.current_night.wolf_target = 'p4'
        human = HumanCliParticipant("你")
        gateway = ParticipantGateway({"p4": human}, VisibilityCompiler(), observer_seat='p4')
        request = gateway._build_base_request(state, 'p4', '请选择是否救人、下毒，或什么都不做。')
        request['options'] = [{'action_type':'NO_OP','targets':[],'requires_target':False,'description':'今晚不使用技能'}]
        stream = io.StringIO()
        with patch.object(builtins, 'input', return_value='1'), redirect_stdout(stream):
            human.decide(request)
        output = stream.getvalue()
        self.assertIn('本规则女巫不能自救', output)

    def test_gateway_actor_never_hides_own_private_night_request(self) -> None:
        state = self._state()
        state.players['p4'].role = Role.WITCH
        state.phase = Phase.WITCH_ACTION
        human = HumanCliParticipant('你')
        gateway = ParticipantGateway({'p4': human}, VisibilityCompiler(), observer_seat='p4')
        request = gateway._build_base_request(state, 'p4', '请选择是否救人、下毒，或什么都不做。')
        self.assertFalse(gateway._should_hide_request_details(state, 'p4', request))



if __name__ == "__main__":
    unittest.main()
