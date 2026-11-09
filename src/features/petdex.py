"""
Petdex Animated Terminal Mascot & Companion Engine (Hermes Style)
Reactive Unicode companion with leveling, experience points (EXP), evolutions,
and cheering animations upon passing test suites and tool completions.
"""

from dataclasses import dataclass
import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional


@dataclass
class Pet:
    slug: str
    name: str
    stage: str
    ascii_art: str
    cheer_art: str
    perk: str


PET_REGISTRY: Dict[str, Pet] = {
    "lobster": Pet(
        slug="lobster",
        name="Omni Lobster",
        stage="Ancient Crustacean",
        ascii_art="""( •_•)
( >🦞)>
 /   \\""",
        cheer_art="""( ＾◡＾)っ✂️
"CLAW-SOME WORK! Tests all passing!"
 🦞 🦞 🦞""",
        perk="+10% Tool Execution Speed",
    ),
    "phoenix": Pet(
        slug="phoenix",
        name="Solar Phoenix",
        stage="Ascendant Flame",
        ascii_art="""  /\\_/\\  
 ( o.o ) 
  > ^ <  """,
        cheer_art="""  \\o/  ✨ ✦ RISE FROM THE ASHES ✦ ✨
   |    All tests green! Zero syntax errors!
  / \\""",
        perk="+15% Self-Evolution Mutation Quality",
    ),
    "dragon": Pet(
        slug="dragon",
        name="Cyber Dragon",
        stage="Neon Wyrm",
        ascii_art="""  ^---^
 ( 'o' )
 (  u  )""",
        cheer_art=""" 🔥🐉 ROAAAR! Flawless turn execution!
 [EXP BOOST +50]""",
        perk="+20% Memory Retention Assertions",
    ),
    "cat": Pet(
        slug="cat",
        name="Meowpi",
        stage="Console Kitten",
        ascii_art=""" ฅ^•ﻌ•^ฅ
 (   )
  " " """,
        cheer_art=""" ฅ(＾・ω・＾)ฅ Purrrfect commit! Everything is verified!""",
        perk="+5% Context Compaction Ratio",
    ),
}


class PetdexManager:
    def __init__(self, config_path: Optional[str] = None):
        self.config_path = config_path or os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
            "omni.config.json",
        )
        self.active_pet_slug = "lobster"
        self.exp = 0
        self._load()

    def _load(self):
        cfg = Path(self.config_path)
        if cfg.exists():
            try:
                with open(cfg, "r", encoding="utf-8") as f:
                    data = json.load(f)
                self.active_pet_slug = data.get("pet_slug", "lobster")
                self.exp = data.get("pet_exp", 0)
            except Exception:
                pass

    def _save(self):
        cfg = Path(self.config_path)
        try:
            data = {}
            if cfg.exists():
                with open(cfg, "r", encoding="utf-8") as f:
                    data = json.load(f)
            data["pet_slug"] = self.active_pet_slug
            data["pet_exp"] = self.exp
            with open(cfg, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
        except Exception:
            pass

    @property
    def level(self) -> int:
        return 1 + (self.exp // 100)

    def gain_exp(self, points: int) -> Dict[str, Any]:
        """Awards experience points, returns level-up status."""
        old_lvl = self.level
        self.exp += points
        new_lvl = self.level
        self._save()
        leveled_up = new_lvl > old_lvl
        return {
            "exp_gained": points,
            "total_exp": self.exp,
            "level": new_lvl,
            "leveled_up": leveled_up,
        }

    def get_active_pet(self) -> Pet:
        return PET_REGISTRY.get(self.active_pet_slug, PET_REGISTRY["lobster"])

    def switch_pet(self, slug: str) -> bool:
        s = slug.lower().strip()
        if s in PET_REGISTRY:
            self.active_pet_slug = s
            self._save()
            return True
        return False

    def hatch_random(self) -> Pet:
        """Hatches or unlocks a companion pet."""
        import random
        slugs = list(PET_REGISTRY.keys())
        chosen = random.choice(slugs)
        self.active_pet_slug = chosen
        self.gain_exp(50)
        return PET_REGISTRY[chosen]

    def render_hud(self) -> str:
        """Renders the pet companion for the terminal HUD."""
        pet = self.get_active_pet()
        return (
            f"\n--- Petdex Active Companion ---\n"
            f"  {pet.name} (Level {self.level} | {self.exp % 100}/100 EXP to next)\n"
            f"  Stage: {pet.stage} | Perk: {pet.perk}\n\n"
            f"{pet.ascii_art}\n"
        )

    def cheer(self, reason: str = "test_passed") -> str:
        """Returns a celebratory mascot animation string."""
        pet = self.get_active_pet()
        self.gain_exp(25)
        return (
            f"\n{'=' * 50}\n"
            f"  [PETDEX COMPANION CHEER]\n"
            f"{pet.cheer_art}\n"
            f"  Reason: {reason} (+25 EXP! Total: {self.exp} | Level {self.level})\n"
            f"{'=' * 50}\n"
        )
