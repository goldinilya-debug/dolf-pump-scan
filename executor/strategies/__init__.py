"""Strategy registry. Enable with .env flags STRAT_<NAME>=1 (S06 on by default)."""
from __future__ import annotations

from .base import Close, Info, MoveStop, NotReady, Open, Strategy, Target  # noqa: F401
from .donchian import Donchian
from .pumps import Pumps
from .s06_tsmom import S06
from .star import Star

REGISTRY = {"S06": S06, "DONCHIAN": Donchian, "STAR": Star, "PUMPS": Pumps}
DEFAULT_ON = {"S06": True, "DONCHIAN": False, "STAR": False, "PUMPS": False}


def flag(env: dict, name: str) -> bool:
    v = str(env.get(f"STRAT_{name}", "")).strip().lower()
    return DEFAULT_ON[name] if v == "" else v in ("1", "true", "yes", "on")


def load_enabled(env: dict) -> list[Strategy]:
    return [cls(env) for name, cls in REGISTRY.items() if flag(env, name)]
