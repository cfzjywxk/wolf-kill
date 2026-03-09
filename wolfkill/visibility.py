from __future__ import annotations

from .models import GameState, Phase, Role, team_for_role


class VisibilityCompiler:
    def __init__(self, full_context_event_limit: int = 36) -> None:
        self.full_context_event_limit = max(1, int(full_context_event_limit))

    def _recent_events(self, events: list, *, limit: int | None = None) -> tuple[list, int]:
        effective_limit = self.full_context_event_limit if limit is None else max(1, int(limit))
        if len(events) <= effective_limit:
            return events, 0
        omitted = len(events) - effective_limit
        return events[-effective_limit:], omitted

    def public_state(self, state: GameState, *, since_event_id: int = 0) -> dict:
        all_public = state.public_transcript()
        if since_event_id > 0:
            events = [event for event in all_public if event.index > since_event_id]
            omitted = 0
        else:
            events, omitted = self._recent_events(all_public)
        result: dict = {
            "day": state.day,
            "phase": state.phase.value,
            "seat_order": list(state.seat_order),
            "alive_seats": state.living_seats(),
            "dead_players": [
                {
                    "seat": player.seat,
                    "name": player.name,
                    "death_day": player.death_day,
                }
                for player in state.players.values() if not player.alive
            ],
            "winner": state.winner.value if state.winner else None,
        }
        if state.phase.value == "DAY_SPEECH":
            result["day_speaking_order"] = state.living_seats()
        if since_event_id > 0:
            result["new_public_events"] = [event.to_dict() for event in events]
        else:
            result["preset"] = state.preset_name
            result["players"] = [{"seat": player.seat, "name": player.name, "alive": player.alive} for player in state.players.values()]
            result["public_event_count"] = len(all_public)
            if omitted > 0:
                result["omitted_public_event_count"] = omitted
                result["history_truncated"] = True
        return result

    def private_view(self, state: GameState, seat: str, *, since_event_id: int = 0) -> dict:
        player = state.players[seat]
        all_visible = [event for event in state.transcript if event.visible_to(seat)]
        if since_event_id > 0:
            events = [event for event in all_visible if event.index > since_event_id]
            omitted = 0
        else:
            events, omitted = self._recent_events(all_visible)
        view: dict = {
            "seat": seat,
            "role": player.role.value,
            "team": team_for_role(player.role).value,
            "alive": player.alive,
        }
        if since_event_id > 0:
            view["new_visible_events"] = [event.to_dict() for event in events]
        else:
            view["name"] = player.name
            view["teammates"] = []
            view["seer_results"] = [item.to_dict() for item in state.seer_results.get(seat, [])]
            view["witch_resources"] = state.witch_resources.get(seat).to_dict() if seat in state.witch_resources else None
            view["all_visible_events"] = [event.to_dict() for event in events]
            view["visible_event_count"] = len(all_visible)
            if omitted > 0:
                view["omitted_visible_event_count"] = omitted
                view["history_truncated"] = True
        if player.role == Role.WOLF:
            view["teammates"] = [
                {"seat": wolf_seat, "name": state.players[wolf_seat].name, "alive": state.players[wolf_seat].alive}
                for wolf_seat in state.wolves(alive_only=False) if wolf_seat != seat
            ]
        if player.role == Role.WITCH:
            view["witch_resources"] = state.witch_resources.get(seat).to_dict() if seat in state.witch_resources else None
            view["night_hint"] = {"wolf_target": state.current_night.wolf_target} if state.current_night.wolf_target is not None else None
        elif since_event_id == 0:
            view["night_hint"] = None
        if player.role == Role.SEER and state.phase == Phase.DAY_SPEECH:
            latest_inspection = next((item for item in reversed(state.seer_results.get(seat, [])) if item.day == state.day), None)
            if latest_inspection is not None:
                view["seer_claim_constraints"] = {
                    "latest_inspection_day": latest_inspection.day,
                    "latest_inspection_target": latest_inspection.target,
                    "timeline_note": f"你最近一次查验发生在第{state.day}夜，早于第{state.day}天白天全部发言。解释这次验人理由时，不得引用第{state.day}天白天才出现的发言、对跳、归票压力或今天新增的站边信息。",
                    "allowed_reason_examples": ["上一天的投票与发言", "已知死亡/平安夜结构", "固定座位压力", "零信息摸高压位"],
                    "forbidden_reason_examples": ["因为今天1号发言很空所以昨晚去验2号", "为了验证刚才前置位的发言所以昨晚去验人", "为了看今天归票位是不是好人所以昨晚去验后置位"],
                }
        if since_event_id > 0 and player.role == Role.SEER:
            view["seer_results"] = [item.to_dict() for item in state.seer_results.get(seat, [])]
        return view
