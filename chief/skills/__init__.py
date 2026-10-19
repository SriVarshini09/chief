"""Skill loader: every module in this package that defines SKILL is registered."""

from __future__ import annotations

import importlib
import pkgutil

from chief.skills.base import Context, Skill

__all__ = ["Context", "Skill", "load_skills"]


def load_skills() -> dict[str, Skill]:
    found: dict[str, Skill] = {}
    for mod in pkgutil.iter_modules(__path__):
        if mod.name == "base":
            continue
        skill = getattr(importlib.import_module(f"{__name__}.{mod.name}"), "SKILL", None)
        if isinstance(skill, Skill):
            found[skill.name] = skill
    return found
