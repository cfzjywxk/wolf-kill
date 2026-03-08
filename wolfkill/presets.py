from __future__ import annotations

from dataclasses import dataclass
from random import Random

from .models import GameState, PlayerState, Role, WitchResources


@dataclass(frozen=True)
class Preset:
    name: str
    seat_order: tuple[str, ...]
    roles: tuple[Role, ...]


_PRESETS: dict[str, Preset] = {
    "classic-6": Preset(
        name="classic-6",
        seat_order=("p1", "p2", "p3", "p4", "p5", "p6"),
        roles=(Role.WOLF, Role.WOLF, Role.SEER, Role.WITCH, Role.VILLAGER, Role.VILLAGER),
    ),
    "duel-2": Preset(
        name="duel-2",
        seat_order=("p1", "p2"),
        roles=(Role.WOLF, Role.WOLF),
    ),
    "all-wolf-4": Preset(
        name="all-wolf-4",
        seat_order=("p1", "p2", "p3", "p4"),
        roles=(Role.WOLF, Role.WOLF, Role.WOLF, Role.WOLF),
    ),
}


def get_preset(name: str) -> Preset:
    try:
        return _PRESETS[name]
    except KeyError as exc:
        raise KeyError(f"unknown preset: {name}") from exc


def create_state_from_role_map(preset_name: str, seed: int, role_map: dict[str, Role]) -> GameState:
    preset = get_preset(preset_name)
    players = {
        seat: PlayerState(seat=seat, name=seat.upper(), role=role_map[seat])
        for seat in preset.seat_order
    }
    return _build_state(preset, seed, players)


def create_state_from_preset(
    preset_name: str,
    seed: int,
    *,
    names: dict[str, str] | None = None,
    backgrounds: dict[str, str] | None = None,
) -> GameState:
    preset = get_preset(preset_name)
    rng = Random(seed)
    roles = list(preset.roles)
    rng.shuffle(roles)
    players: dict[str, PlayerState] = {}
    for seat, role in zip(preset.seat_order, roles, strict=True):
        players[seat] = PlayerState(
            seat=seat,
            name=(names or {}).get(seat, seat.upper()),
            role=role,
            background=(backgrounds or {}).get(seat),
        )
    return _build_state(preset, seed, players)


def _build_state(preset: Preset, seed: int, players: dict[str, PlayerState]) -> GameState:
    state = GameState(
        preset_name=preset.name,
        seed=seed,
        players=players,
        seat_order=preset.seat_order,
    )
    for seat, player in players.items():
        if player.role == Role.WITCH:
            state.witch_resources[seat] = WitchResources()
        if player.role == Role.SEER:
            state.seer_results[seat] = []
    return state
