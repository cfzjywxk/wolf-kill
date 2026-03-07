from __future__ import annotations

from .models import GameState, Role, team_for_role


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
        if since_event_id > 0 and player.role == Role.SEER:
            view["seer_results"] = [item.to_dict() for item in state.seer_results.get(seat, [])]
        return view
