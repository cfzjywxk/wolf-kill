from __future__ import annotations

import io
import json
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch
from contextlib import redirect_stdout

from wolfkill import colors
from wolfkill.debug_logging import AgentDebugLogger
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

    def test_gateway_writes_agent_debug_log_entries(self) -> None:
        state = self._state()
        state.phase = Phase.DAY_SPEECH
        with tempfile.TemporaryDirectory() as temp_dir:
            log_path = Path(temp_dir) / "agent-debug.jsonl"
            gateway = ParticipantGateway({"p1": GoodParticipant("good")}, VisibilityCompiler(), debug_logger=AgentDebugLogger(log_path))
            event = state.add_event(visibility=EventVisibility.PUBLIC, channel="system", text="开局公开信息")
            with redirect_stdout(io.StringIO()):
                gateway.on_event(event)
                gateway.request_speech(state, "p1", Audience.PUBLIC, "请发表一句简短的白天发言。")
            entries = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines() if line.strip()]

        kinds = [entry["kind"] for entry in entries]
        self.assertIn("event", kinds)
        self.assertIn("agent_call", kinds)
        event_entry = next(entry for entry in entries if entry["kind"] == "event")
        self.assertEqual(event_entry["event"]["text"], "开局公开信息")
        agent_entry = next(entry for entry in entries if entry["kind"] == "agent_call")
        self.assertEqual(agent_entry["seat"], "p1")
        self.assertEqual(agent_entry["request"]["phase"], Phase.DAY_SPEECH.value)
        self.assertEqual(agent_entry["final_response"]["text"], "测试发言")

    def test_bootstrap_sessions_primes_ai_context_and_next_request_is_incremental(self) -> None:
        class SessionParticipant(ParticipantAdapter):
            def __init__(self, name: str):
                super().__init__(name)
                self.calls = []
                self._has_session = True

            @property
            def has_session(self) -> bool:
                return self._has_session

            def speak(self, request):
                self.calls.append(request)
                self.set_last_call_diagnostics(provider="session_participant", mode="speech", context_mode=request.get("context_mode"), prompt_chars=10, response_chars=10, provider_seconds=0.01, io_wait_seconds=0.01, parse_seconds=0.0, total_seconds=0.01)
                self.set_last_call_exchange(mode="speech", prompt="bootstrap or speech", raw_output='{"text":"ok"}', parsed_response={"text":"ok"}, parse_mode="json")
                return {"text": "ok"}

            def decide(self, request):
                self.calls.append(request)
                self.set_last_call_diagnostics(provider="session_participant", mode="decision", context_mode=request.get("context_mode"), prompt_chars=10, response_chars=10, provider_seconds=0.01, io_wait_seconds=0.01, parse_seconds=0.0, total_seconds=0.01)
                self.set_last_call_exchange(mode="decision", prompt="decision", raw_output='{"action_type":"NO_OP","target":null}', parsed_response={"action_type":"NO_OP","target":None}, parse_mode="json")
                return {"action_type": "NO_OP", "target": None}

        state = self._state()
        participant = SessionParticipant("session")
        gateway = ParticipantGateway({"p1": participant}, VisibilityCompiler(), learn_history=["guide"])
        state.add_event(visibility=EventVisibility.PUBLIC, channel="system", text="开局公开信息")

        with redirect_stdout(io.StringIO()):
            gateway.bootstrap_sessions(state)

        self.assertEqual(len(participant.calls), 1)
        self.assertEqual(participant.calls[0]["context_mode"], "full")
        self.assertIn("strategy_briefing", participant.calls[0])

        state.phase = Phase.DAY_SPEECH
        state.add_event(visibility=EventVisibility.PUBLIC, channel="speech", text="新的公开发言", speaker="p2")
        request = gateway._build_base_request(state, "p1", "请发言")
        self.assertEqual(request["context_mode"], "incremental")
        self.assertNotIn("strategy_briefing", request)
        self.assertIn("new_public_events", request["public_state"])

    def test_bootstrap_failure_resets_session_participant_to_full_context(self) -> None:
        class FailingSessionParticipant(ParticipantAdapter):
            def __init__(self, name: str):
                super().__init__(name)
                self._session_id = 1

            @property
            def has_session(self) -> bool:
                return self._session_id is not None

            def reset_state(self) -> None:
                super().reset_state()
                self._session_id = None

            def speak(self, request):
                raise RuntimeError("bootstrap boom")

            def decide(self, request):
                return {"action_type": "NO_OP", "target": None}

        state = self._state()
        participant = FailingSessionParticipant("failing")
        gateway = ParticipantGateway({"p1": participant}, VisibilityCompiler())
        state.add_event(visibility=EventVisibility.PUBLIC, channel="system", text="开局公开信息")

        with redirect_stdout(io.StringIO()):
            gateway.bootstrap_sessions(state)

        request = gateway._build_base_request(state, "p1", "请发言")
        self.assertEqual(request["context_mode"], "full")

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

    def test_human_wolf_chat_can_end_with_numeric_shortcut(self) -> None:
        import builtins
        human = HumanCliParticipant("你")
        request = {
            "seat": "p1",
            "name": "你",
            "day": 1,
            "phase": Phase.WOLF_CHAT.value,
            "phase_label": "狼人密谈",
            "prompt": "【第2轮讨论】请回应队友建议，确认或调整击杀目标；若你已无更多讨论内容，请直接回复：无更多讨论。",
            "private_view": {
                "seat": "p1",
                "role": Role.WOLF.value,
                "team": "WOLF",
                "alive": True,
                "role_label": "狼人",
                "team_label": "狼人阵营",
                "alive_label": "是",
                "teammates": [{"seat": "p2"}],
                "all_visible_events": [],
            },
        }
        with patch.object(builtins, 'input', return_value='2'), redirect_stdout(io.StringIO()):
            response = human.speak(request)
        self.assertEqual(response["text"], "无更多讨论")

    def test_human_day_speech_still_accepts_free_text(self) -> None:
        import builtins
        human = HumanCliParticipant("你")
        request = {
            "seat": "p1",
            "name": "你",
            "day": 1,
            "phase": Phase.DAY_SPEECH.value,
            "phase_label": "白天发言",
            "prompt": "请根据当前局势发表白天发言。",
            "private_view": {
                "seat": "p1",
                "role": Role.VILLAGER.value,
                "team": "VILLAGE",
                "alive": True,
                "role_label": "平民",
                "team_label": "好人阵营",
                "alive_label": "是",
                "all_visible_events": [],
            },
        }
        with patch.object(builtins, 'input', return_value='我有一个判断'), redirect_stdout(io.StringIO()):
            response = human.speak(request)
        self.assertEqual(response["text"], '我有一个判断')

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

    def test_hidden_night_clock_stays_active_until_visible_event(self) -> None:
        state = self._state()
        state.phase = Phase.WOLF_CHAT
        human = HumanCliParticipant('你')
        gateway = ParticipantGateway({'p1': human, 'p2': GoodParticipant('good')}, VisibilityCompiler())
        events: list[str] = []

        def fake_start_clock():
            events.append('start')

        def fake_stop_clock():
            events.append('stop')

        gateway._start_clock = fake_start_clock  # type: ignore[method-assign]
        gateway._stop_clock = fake_stop_clock  # type: ignore[method-assign]
        speech_request = gateway._build_base_request(state, 'p2', '请向狼人队友发送一句简短消息。')
        speech_request['audience'] = Audience.WOLF.value
        with redirect_stdout(io.StringIO()):
            gateway._announce_wait(state, 'p2', 'speech', speech_request)
            gateway._start_wait_clock(hidden_wait=True, day=1)
            gateway._stop_wait_clock(hidden_wait=True)
            gateway._announce_wait(state, 'p2', 'speech', speech_request)
            gateway._start_wait_clock(hidden_wait=True, day=1)
            gateway._stop_wait_clock(hidden_wait=True)
            state.add_event(visibility=EventVisibility.PUBLIC, channel='system', text='天亮了')
            gateway.on_event(state.transcript[-1])
        self.assertEqual(events.count('start'), 1)
        self.assertEqual(events.count('stop'), 1)



if __name__ == "__main__":
    unittest.main()
