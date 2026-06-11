"""EmberQuest game logic — static catalogs and pure balance math.

No SDK imports, no ctx, no I/O. Every random outcome flows through an injected
``random.Random`` so the whole module is deterministic under test.

All names, mobs, and locations are original EmberQuest IP.
"""

from __future__ import annotations

import hashlib
import random as _random_module  # only for Random construction with explicit seeds
import time

CURRENCY = "Embers"
VIRTUAL_NOTE = "Embers are virtual in-game currency with no real-world value."

# Cooldowns (seconds). Enforced lazily against SQL timestamps on each command;
# ctx.ephemeral is only ever a fast spam guard on top of these.
HUNT_COOLDOWN = 60
ADVENTURE_COOLDOWN = 3600
DAILY_COOLDOWN = 86400

REGEN_SECONDS_PER_HP = 120
BASE_MAX_HP = 100
MAX_HP_PER_LEVEL = 10
DEFEAT_COIN_LOSS = 0.10  # fraction of carried Embers lost when knocked out
SELL_RATIO = 0.5
MIN_BET = 10
LOW_HP_FRACTION = 0.35  # below this, combat responses offer a Heal button

DEFAULT_SWORD = "fists"
DEFAULT_ARMOR = "cloth"
LIFE_POTION = "ember_tonic"
GREATER_LIFE_POTION = "phoenix_draught"
LIFE_POTION_HEAL = 50

ITEMS = {
    # Swords — atk raises win chance and shaves incoming damage.
    "fists": {"name": "Fists", "kind": "sword", "atk": 0, "price": None, "emoji": "✊"},
    "charstick": {"name": "Charstick", "kind": "sword", "atk": 3, "price": 150, "emoji": "🪵"},
    "cinder_saber": {"name": "Cinder Saber", "kind": "sword", "atk": 7, "price": 600, "emoji": "🗡️"},
    "ironspark_blade": {"name": "Ironspark Blade", "kind": "sword", "atk": 12, "price": 2000, "emoji": "⚔️"},
    "emberglass_edge": {"name": "Emberglass Edge", "kind": "sword", "atk": 18, "price": 6000, "emoji": "🔶"},
    "pyrelord_greatsword": {"name": "Pyrelord Greatsword", "kind": "sword", "atk": 25, "price": 15000, "emoji": "🔥"},
    # Armor — defense reduces incoming damage.
    "cloth": {"name": "Traveler's Cloth", "kind": "armor", "defense": 0, "price": None, "emoji": "🧣"},
    "padded_vest": {"name": "Padded Vest", "kind": "armor", "defense": 2, "price": 120, "emoji": "🦺"},
    "kindled_mail": {"name": "Kindled Mail", "kind": "armor", "defense": 5, "price": 500, "emoji": "🪖"},
    "ashforged_plate": {"name": "Ashforged Plate", "kind": "armor", "defense": 9, "price": 1800, "emoji": "🛡️"},
    "emberweave_aegis": {"name": "Emberweave Aegis", "kind": "armor", "defense": 14, "price": 5000, "emoji": "✨"},
    # Consumables.
    LIFE_POTION: {"name": "Emberbloom Tonic", "kind": "consumable", "heal": LIFE_POTION_HEAL, "price": 80, "emoji": "🧪"},
    GREATER_LIFE_POTION: {"name": "Phoenix Draught", "kind": "consumable", "heal": None, "price": 250, "emoji": "⚗️"},
    # Materials — drop from dungeons (and rarely hunts/adventures); never sold
    # in the shop (price None), but the Emberforge buys them back ("sell").
    "ember_shard": {"name": "Ember Shard", "kind": "material", "price": None, "sell": 15, "emoji": "✴️"},
    "wraithsilk": {"name": "Wraithsilk", "kind": "material", "price": None, "sell": 40, "emoji": "🕸️"},
    "ashsteel_ingot": {"name": "Ashsteel Ingot", "kind": "material", "price": None, "sell": 120, "emoji": "🔩"},
    "magmaheart": {"name": "Magmaheart", "kind": "material", "price": None, "sell": 400, "emoji": "🌋"},
    # Craft-only gear — above the shop's top tier; never buyable.
    "dawnforged_blade": {"name": "Dawnforged Blade", "kind": "sword", "atk": 30, "price": None, "sell": 4000, "emoji": "🌅"},
    "wraithsilk_shroud": {"name": "Wraithsilk Shroud", "kind": "armor", "defense": 17, "price": None, "sell": 3200, "emoji": "🌫️"},
    # Lootboxes ("caches") — open with /open. Odds are published in-game.
    "ember_cache": {"name": "Ember Cache", "kind": "lootbox", "price": 400, "emoji": "📦"},
    "blazing_cache": {"name": "Blazing Cache", "kind": "lootbox", "price": None, "sell": 600, "emoji": "🎁"},
    # Companions — hatch from caches; one walks beside you (/pet). Tradeable.
    "cinderpup": {"name": "Cinderpup", "kind": "pet", "price": None, "sell": 1000, "emoji": "🐕"},
    "ashwhisker": {"name": "Ashwhisker", "kind": "pet", "price": None, "sell": 1000, "emoji": "🐈"},
    "wisplight": {"name": "Wisplight", "kind": "pet", "price": None, "sell": 1000, "emoji": "🕯️"},
    "pyrelet": {"name": "Pyrelet", "kind": "pet", "price": None, "sell": 1000, "emoji": "🦎"},
    "emberowl": {"name": "Emberowl", "kind": "pet", "price": None, "sell": 1000, "emoji": "🦉"},
}

MOBS = [
    {"id": "cinder_slime", "name": "Cinder Slime", "tier": 1, "min_level": 1},
    {"id": "ash_hare", "name": "Ash Hare", "tier": 1, "min_level": 1},
    {"id": "soot_imp", "name": "Soot Imp", "tier": 2, "min_level": 3},
    {"id": "glowmoth", "name": "Glowmoth", "tier": 2, "min_level": 5},
    {"id": "charhound", "name": "Charhound", "tier": 3, "min_level": 8},
    {"id": "wick_wraith", "name": "Wick Wraith", "tier": 3, "min_level": 12},
    {"id": "magmite", "name": "Magmite", "tier": 4, "min_level": 16},
    {"id": "pyreclaw", "name": "Pyreclaw", "tier": 5, "min_level": 20},
]

ADVENTURES = [
    {"id": "smoldering_fen", "name": "the Smoldering Fen", "tier": 1, "min_level": 1},
    {"id": "ashfall_ruins", "name": "the Ashfall Ruins", "tier": 2, "min_level": 5},
    {"id": "glasswastes", "name": "the Glasswastes", "tier": 3, "min_level": 10},
    {"id": "caldera_throat", "name": "the Caldera Throat", "tier": 4, "min_level": 15},
]

# --- Dungeons (Phase 2): party expeditions against minibosses -----------------

DUNGEON_MIN_PARTY = 2
DUNGEON_MAX_PARTY = 4
DUNGEON_COOLDOWN = 7200       # per player, claimed at resolution (not at join)
DUNGEON_LOBBY_TTL = 900       # open lobbies go cold after 15 minutes (lazy expiry)

# drops: (item_id, probability, min_count, max_count) rolled per member on a win.
DUNGEONS = {
    "kiln": {
        "name": "the Smelterdeep", "boss": "the Kilnwarden",
        "min_level": 3, "tier": 2, "power": 60,
        "drops": [("ember_shard", 0.9, 1, 2), ("wraithsilk", 0.5, 1, 1),
                  ("ember_cache", 0.12, 1, 1)],
    },
    "cinderkeep": {
        "name": "the Sunken Cinderkeep", "boss": "the Drowned Ember-Knight",
        "min_level": 8, "tier": 3, "power": 150,
        "drops": [("ember_shard", 0.9, 1, 3), ("wraithsilk", 0.6, 1, 2),
                  ("ashsteel_ingot", 0.5, 1, 1), ("blazing_cache", 0.10, 1, 1)],
    },
    "crucible": {
        "name": "the Pyrelord's Crucible", "boss": "the Avatar of the Pyrelord",
        "min_level": 15, "tier": 5, "power": 300,
        "drops": [("wraithsilk", 0.5, 1, 2), ("ashsteel_ingot", 0.7, 1, 2),
                  ("magmaheart", 0.35, 1, 1), ("blazing_cache", 0.18, 1, 1)],
    },
}

# --- Crafting (Phase 2) --------------------------------------------------------

RECIPES = {
    LIFE_POTION: {"materials": {"ember_shard": 1}, "fee": 20},
    GREATER_LIFE_POTION: {"materials": {"ember_shard": 2, "wraithsilk": 1}, "fee": 100},
    "dawnforged_blade": {"materials": {"ashsteel_ingot": 3, "magmaheart": 1}, "fee": 2500},
    "wraithsilk_shroud": {"materials": {"wraithsilk": 4, "ashsteel_ingot": 2}, "fee": 2000},
}

# --- Enchanting (Phase 2) --------------------------------------------------------

ENCHANT_MAX = 5
ENCHANT_BONUS = 2             # +atk or +def per enchant level
ENCHANT_MATERIAL = "ember_shard"

# --- PvP duels (Phase 3) -----------------------------------------------------------

DUEL_TTL = 300                # an unanswered challenge goes cold after 5 minutes
DUEL_COOLDOWN = 600           # per player, set at resolution for both fighters
DUEL_XP_WIN = 25
DUEL_XP_LOSS = 10
DUEL_MAX_BET = 1_000_000

# --- Trading (Phase 3) ---------------------------------------------------------------

TRADE_TTL = 600               # an unanswered offer goes cold after 10 minutes
TRADE_MAX_QTY = 25
TRADE_MAX_PRICE = 10_000_000

# --- Guilds (Phase 3) -----------------------------------------------------------------

GUILD_CREATE_COST = 2500      # Ember sink
GUILD_MAX_MEMBERS = 20
GUILD_NAME_MIN = 3
GUILD_NAME_MAX = 24
_GUILD_NAME_CHARS = set("abcdefghijklmnopqrstuvwxyz"
                        "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 '-")

# --- Daily arena (Phase 3) ---------------------------------------------------------------

ARENA_FEE = 50
ARENA_BONUS = 100             # house Embers added to the pool, scaled by multiplier
ARENA_SPLITS = (0.5, 0.3, 0.2)
ARENA_XP = 15

# --- Pets (Phase 4) -------------------------------------------------------------------------

# One active companion per hero; each grants a single passive perk. Values are
# the perk magnitudes consumed by player_boosts() / arena scoring / quests.
PET_PERKS = {
    "cinderpup": {"label": "+10% Embers from hunts & adventures", "coins": 0.10},
    "ashwhisker": {"label": "+10% XP from hunts & adventures", "xp": 0.10},
    "wisplight": {"label": "+8% material drop chance", "drop": 0.08},
    "pyrelet": {"label": "+10 arena score", "arena": 10},
    "emberowl": {"label": "+25% quest rewards", "quest": 0.25},
}

# --- Lootboxes (Phase 4) ----------------------------------------------------------------------

DUPLICATE_PET_COINS = 800     # a cache pet you already own converts to Embers

# Weighted tables (weights sum to 100 — shown verbatim by /open as the odds).
# Entry shapes: ("coins", lo, hi) · ("item", item_id, lo, hi) · ("pet",) ·
# ("jackpot", lo, hi)
LOOT_TABLES = {
    "ember_cache": [
        (34, ("coins", 150, 450)),
        (16, ("item", "ember_shard", 1, 3)),
        (10, ("item", "wraithsilk", 1, 2)),
        (6, ("item", "ashsteel_ingot", 1, 1)),
        (18, ("item", LIFE_POTION, 1, 2)),
        (8, ("item", GREATER_LIFE_POTION, 1, 1)),
        (4, ("pet",)),
        (3, ("item", "blazing_cache", 1, 1)),
        (1, ("jackpot", 2000, 3000)),
    ],
    "blazing_cache": [
        (24, ("coins", 600, 1400)),
        (16, ("item", "wraithsilk", 2, 4)),
        (16, ("item", "ashsteel_ingot", 1, 3)),
        (10, ("item", "magmaheart", 1, 1)),
        (14, ("item", GREATER_LIFE_POTION, 1, 2)),
        (12, ("pet",)),
        (8, ("item", "ember_cache", 1, 2)),
    ],
}
assert all(sum(w for w, _ in table) == 100 for table in LOOT_TABLES.values())

# --- Daily quests (Phase 4) ----------------------------------------------------------------------

QUESTS_PER_DAY = 3
QUEST_XP = 25
QUEST_PRUNE_DAYS = 7

# event_key is what handlers report progress against; target is (lo, hi);
# reward is base Embers (plus 10×level at claim, plus pet/season bonuses).
QUEST_TYPES = {
    "hunt_win": {"desc": "Best {n} monsters with /hunt", "target": (4, 8), "reward": 140},
    "adventure_win": {"desc": "Return victorious from {n} adventure(s)", "target": (1, 2), "reward": 160},
    "dungeon": {"desc": "Survive {n} dungeon expedition(s)", "target": (1, 1), "reward": 250},
    "duel": {"desc": "Cross blades in {n} duel(s)", "target": (1, 2), "reward": 150},
    "craft": {"desc": "Forge {n} item(s) at the Emberforge", "target": (1, 2), "reward": 130},
    "coinflip": {"desc": "Flip {n} coinflip(s)", "target": (2, 4), "reward": 100},
    "arena": {"desc": "Step onto the arena sand", "target": (1, 1), "reward": 120},
    "heal": {"desc": "Drink {n} healing tonic(s)", "target": (1, 2), "reward": 90},
}

# --- Rekindling: prestige (Phase 4) -----------------------------------------------------------------

REKINDLE_MIN_LEVEL = 20
REKINDLE_BONUS_PER = 0.10     # permanent +coins/+xp per Rekindling
REKINDLE_BONUS_CAP = 5        # bonuses stop stacking past five flames

# --- Seasonal events (Phase 4) ------------------------------------------------------------------------

# Stateless UTC date windows — no scheduling needed, pure date math. (start)
# and (end) are inclusive (month, day); a window may wrap the new year.
SEASONS = [
    {"key": "ashbloom", "name": "the Ashbloom", "emoji": "🌸",
     "start": (4, 1), "end": (4, 14), "boost": 0.25},
    {"key": "highsun", "name": "the Highsun Pyre", "emoji": "☀️",
     "start": (6, 10), "end": (6, 24), "boost": 0.25},
    {"key": "emberfall", "name": "the Emberfall Festival", "emoji": "🍂",
     "start": (10, 20), "end": (11, 3), "boost": 0.25},
    {"key": "frostflame", "name": "the Frostflame Vigil", "emoji": "❄️",
     "start": (12, 20), "end": (1, 3), "boost": 0.25},
]
SEASON_MOB_CHANCE = 0.10      # chance a hunt meets the season's bonus creature
SEASON_MOB_BONUS = 1.5        # its rewards multiplier
SEASON_MOBS = {
    "ashbloom": "Bloomwisp",
    "highsun": "Sunscale Basilisk",
    "emberfall": "Lanternshade",
    "frostflame": "Rimefire Wisp",
}


# --- Leveling ---------------------------------------------------------------

def xp_for_level(level: int) -> int:
    """Cumulative XP required to *be* `level`. Level 1 starts at 0."""
    return 50 * level * (level - 1)


def level_from_xp(xp: int) -> int:
    level = 1
    while xp >= xp_for_level(level + 1):
        level += 1
    return level


def max_hp_for_level(level: int) -> int:
    return BASE_MAX_HP + MAX_HP_PER_LEVEL * (level - 1)


def xp_progress(level: int, xp: int) -> tuple[int, int]:
    """(xp gained within the current level, xp span of the current level)."""
    floor = xp_for_level(level)
    return xp - floor, xp_for_level(level + 1) - floor


def regen_hp(hp: int, max_hp: int, last_action_at: int, now: int) -> int:
    """Lazy regen: +1 HP per REGEN_SECONDS_PER_HP since the last action."""
    if last_action_at <= 0 or now <= last_action_at:
        return min(hp, max_hp)
    return min(max_hp, hp + (now - last_action_at) // REGEN_SECONDS_PER_HP)


# --- Encounters -------------------------------------------------------------

def _eligible(catalog, level):
    return [entry for entry in catalog if level >= entry["min_level"]]


def resolve_hunt(level, atk, defense, hp, coins, rng, multiplier, boosts=None):
    """One /hunt. RNG call order is fixed: choice, [season roll], random,
    randints, drop roll. `boosts` comes from player_boosts() (or None)."""
    boosts = boosts or {}
    mob = rng.choice(_eligible(MOBS, level))
    tier = mob["tier"]
    seasonal = False
    if boosts.get("season_mob") and rng.random() < SEASON_MOB_CHANCE:
        seasonal = True
        mob = {"id": "seasonal", "name": boosts["season_mob"],
               "tier": tier, "min_level": mob["min_level"]}
    reward_mult = SEASON_MOB_BONUS if seasonal else 1.0
    win_chance = min(0.95, 0.78 + 0.005 * atk + 0.01 * max(0, level - mob["min_level"]))
    win = rng.random() < win_chance
    if win:
        coins_gain = max(1, round(rng.randint(10, 18) * tier * multiplier
                                  * boosts.get("coins", 1.0) * reward_mult))
        xp_gain = max(1, round(rng.randint(10, 16) * tier
                               * boosts.get("xp", 1.0) * reward_mult))
        damage = max(0, rng.randint(2, 6) * tier - defense)
    else:
        coins_gain = 0
        xp_gain = 0
        damage = max(1, rng.randint(6, 12) * tier - defense)
    result = _settle(mob, win, coins_gain, xp_gain, damage, hp, coins)
    result["seasonal"] = seasonal
    result["drop"] = None
    if result["win"] and rng.random() < 0.07 + boosts.get("drop", 0.0):
        result["drop"] = "ember_shard"  # rare solo trickle toward crafting
    return result


def resolve_adventure(level, atk, defense, hp, coins, rng, multiplier, boosts=None):
    """One /adventure: bigger band than hunt, plus a potion drop chance."""
    boosts = boosts or {}
    place = rng.choice(_eligible(ADVENTURES, level))
    tier = place["tier"]
    win_chance = min(0.92, 0.70 + 0.006 * atk + 0.01 * max(0, level - place["min_level"]))
    win = rng.random() < win_chance
    drop = None
    if win:
        coins_gain = max(1, round(rng.randint(60, 110) * tier * multiplier
                                  * boosts.get("coins", 1.0)))
        xp_gain = max(1, round(rng.randint(55, 85) * tier * boosts.get("xp", 1.0)))
        damage = max(0, rng.randint(5, 12) * tier - defense)
        roll = rng.random()
        if roll < 0.05:
            drop = GREATER_LIFE_POTION
        elif roll < 0.30:
            drop = LIFE_POTION
    else:
        coins_gain = 0
        xp_gain = 0
        damage = max(1, rng.randint(12, 20) * tier - defense)
    material = None
    if win:
        drop_bonus = boosts.get("drop", 0.0)
        material_roll = rng.random()
        if material_roll < 0.15 + drop_bonus / 2:
            material = "wraithsilk"
        elif material_roll < 0.40 + drop_bonus:
            material = "ember_shard"
    result = _settle(place, win, coins_gain, xp_gain, damage, hp, coins)
    result["drop"] = None if result["defeated"] else drop
    result["material"] = None if result["defeated"] else material
    return result


def _settle(foe, win, coins_gain, xp_gain, damage, hp, coins):
    defeated = damage >= hp
    if defeated:
        # Knocked out: HP floors at 1, a cut of carried Embers is lost, no spoils.
        return {
            "foe": foe, "win": False, "defeated": True, "damage": damage,
            "hp_after": 1, "coins_delta": -int(coins * DEFEAT_COIN_LOSS), "xp_gain": 0,
        }
    return {
        "foe": foe, "win": win, "defeated": False, "damage": damage,
        "hp_after": hp - damage, "coins_delta": coins_gain, "xp_gain": xp_gain,
    }


def resolve_dungeon(member_stats, dungeon, rng, multiplier):
    """Resolve a party expedition. RNG order is fixed: party roll, win roll,
    then per-member rolls in roster order (randints, then drop rolls).

    member_stats: [{"user_id", "username", "level", "atk", "defense", "hp"}, ...]
    Defeated members are dragged out at 1 HP and keep half loot.
    """
    tier = dungeon["tier"]
    party_power = sum(m["level"] * 3 + m["atk"] * 2 + 10 for m in member_stats)
    party_power += rng.randint(0, 10)
    win_chance = min(0.92, max(0.25, party_power / (party_power + dungeon["power"])))
    win = rng.random() < win_chance

    outcomes = []
    for member in member_stats:
        if win:
            damage = max(0, rng.randint(3, 9) * tier - member["defense"])
            coins = max(1, round(rng.randint(80, 140) * tier * multiplier))
            xp = rng.randint(70, 110) * tier
        else:
            damage = max(1, rng.randint(10, 18) * tier - member["defense"])
            coins = 0
            xp = 0
        defeated = damage >= member["hp"]
        hp_after = 1 if defeated else member["hp"] - damage
        drops = []
        if win:
            for item_id, probability, lo, hi in dungeon["drops"]:
                if rng.random() < probability:
                    drops.append((item_id, rng.randint(lo, hi)))
        if defeated:
            coins //= 2
            xp //= 2
            drops = drops[:1]  # dragged out unconscious: most of the haul is lost
        outcomes.append({
            "user_id": member["user_id"], "username": member["username"],
            "damage": damage, "hp_after": hp_after, "defeated": defeated,
            "coins": coins, "xp": xp, "drops": drops,
        })
    return {"win": win, "win_chance": win_chance, "outcomes": outcomes}


# --- PvP duels -------------------------------------------------------------------

def duel_power(level: int, atk: int, defense: int) -> int:
    return level * 3 + atk * 2 + defense + 5


def resolve_duel(challenger: dict, target: dict, rng) -> dict:
    """Honor bout: no HP at stake, the pot and pride change hands.

    challenger/target: {"level", "atk", "defense"} (effective stats).
    The underdog always keeps a fighting chance (clamped 15–85%).
    """
    power_c = duel_power(challenger["level"], challenger["atk"], challenger["defense"])
    power_t = duel_power(target["level"], target["atk"], target["defense"])
    chance = min(0.85, max(0.15, power_c / (power_c + power_t)))
    return {"challenger_wins": rng.random() < chance, "chance": chance}


# --- Guilds -----------------------------------------------------------------------

def guild_key(name: str) -> str:
    return _normalize(name)


def validate_guild_name(name: str) -> str | None:
    """None if the name is acceptable, else a player-facing error."""
    name = str(name or "").strip()
    if len(name) < GUILD_NAME_MIN or len(name) > GUILD_NAME_MAX:
        return (f"Guild names run {GUILD_NAME_MIN}–{GUILD_NAME_MAX} characters "
                f"(yours is {len(name)}).")
    if not set(name) <= _GUILD_NAME_CHARS:
        return "Guild names may use letters, numbers, spaces, apostrophes, and dashes."
    if not any(c.isalnum() for c in name):
        return "A guild name needs at least one letter or number."
    return None


# --- Daily arena ---------------------------------------------------------------------

def arena_day(ts: int) -> str:
    """UTC calendar day bucket for arena entries, e.g. '2026-06-11'."""
    return time.strftime("%Y-%m-%d", time.gmtime(ts))


def arena_score(level: int, atk: int, defense: int, rng) -> int:
    return level * 3 + atk * 2 + defense + rng.randint(0, 50)


def arena_pool(total_fees: int, multiplier: float) -> int:
    return int(total_fees) + max(0, round(ARENA_BONUS * multiplier))


def arena_payouts(pool: int) -> list[int]:
    """Podium shares (1st, 2nd, 3rd); never exceeds the pool in total."""
    return [int(pool * share) for share in ARENA_SPLITS]


# --- Seasons & boosts (Phase 4) -----------------------------------------------------

def current_season(ts: int) -> dict | None:
    """The active seasonal event for a UTC timestamp, or None."""
    t = time.gmtime(ts)
    today = (t.tm_mon, t.tm_mday)
    for season in SEASONS:
        if season["start"] <= season["end"]:
            if season["start"] <= today <= season["end"]:
                return season
        elif today >= season["start"] or today <= season["end"]:  # wraps new year
            return season
    return None


def player_boosts(pet_id: str | None, rekindles: int, season: dict | None) -> dict:
    """Combined passive multipliers from companion, Rekindlings, and season."""
    perks = PET_PERKS.get(pet_id or "", {})
    flames = min(max(int(rekindles or 0), 0), REKINDLE_BONUS_CAP)
    rekindle_mult = 1 + REKINDLE_BONUS_PER * flames
    season_mult = 1 + (season["boost"] if season else 0.0)
    return {
        "coins": (1 + perks.get("coins", 0.0)) * rekindle_mult * season_mult,
        "xp": (1 + perks.get("xp", 0.0)) * rekindle_mult * season_mult,
        "drop": perks.get("drop", 0.0),
        "arena": perks.get("arena", 0),
        "quest": perks.get("quest", 0.0),
        "season": season,
        "season_mob": SEASON_MOBS.get(season["key"]) if season else None,
    }


# --- Lootboxes -----------------------------------------------------------------------

def roll_lootbox(box_id: str, rng, owned_pet_ids) -> dict:
    """Open one cache. RNG order: one random() for the slot, then any randint
    for amounts, then choice() if a pet is drawn. Duplicate pets convert to
    Embers so a cache is never a dead pull."""
    table = LOOT_TABLES[box_id]
    roll = rng.random() * 100
    cumulative = 0
    entry = table[-1][1]
    for weight, candidate in table:
        cumulative += weight
        if roll < cumulative:
            entry = candidate
            break
    kind = entry[0]
    if kind in ("coins", "jackpot"):
        return {"kind": "coins", "amount": rng.randint(entry[1], entry[2]),
                "jackpot": kind == "jackpot", "duplicate_pet": None}
    if kind == "item":
        return {"kind": "item", "item_id": entry[1],
                "count": rng.randint(entry[2], entry[3])}
    pet = rng.choice(sorted(PET_PERKS))
    if pet in set(owned_pet_ids):
        return {"kind": "coins", "amount": DUPLICATE_PET_COINS,
                "jackpot": False, "duplicate_pet": pet}
    return {"kind": "pet", "item_id": pet}


def lootbox_odds(box_id: str) -> list[tuple[int, str]]:
    """(percent, label) rows for honest in-game odds disclosure."""
    rows = []
    for weight, entry in LOOT_TABLES[box_id]:
        kind = entry[0]
        if kind == "coins":
            label = f"{entry[1]}–{entry[2]} {CURRENCY}"
        elif kind == "jackpot":
            label = f"JACKPOT: {entry[1]}–{entry[2]} {CURRENCY}"
        elif kind == "item":
            item = ITEMS[entry[1]]
            count = f" ×{entry[2]}–{entry[3]}" if entry[3] > entry[2] else f" ×{entry[2]}"
            label = f"{item['emoji']} {item['name']}{count}"
        else:
            label = "🐾 a companion (duplicates become "
            label += f"{DUPLICATE_PET_COINS} {CURRENCY})"
        rows.append((weight, label))
    return rows


# --- Daily quests -----------------------------------------------------------------------

def daily_quests(user_id: str, day: str) -> list[dict]:
    """The day's three quests for a hero — pure function of (day, user_id),
    so the board needs no storage beyond progress counters."""
    seed = int.from_bytes(
        hashlib.sha256(f"{day}:{user_id}".encode()).digest()[:8], "big")
    rng = _random_module.Random(seed)
    keys = rng.sample(sorted(QUEST_TYPES), QUESTS_PER_DAY)
    quests = []
    for key in keys:
        spec = QUEST_TYPES[key]
        target = rng.randint(*spec["target"])
        quests.append({"key": key, "target": target,
                       "desc": spec["desc"].format(n=target),
                       "reward": spec["reward"]})
    return quests


def quest_reward(quest: dict, level: int, boosts: dict | None = None) -> int:
    base = quest["reward"] + 10 * level
    bonus = (boosts or {}).get("quest", 0.0)
    return max(1, round(base * (1 + bonus)))


# --- Enchanting ----------------------------------------------------------------

def enchant_cost(next_level: int) -> dict:
    return {"coins": 100 * next_level * next_level, "materials": next_level}


def enchant_success_chance(current_level: int) -> float:
    return max(0.25, 0.95 - 0.15 * current_level)


def enchant_attempt(current_level: int, rng) -> bool:
    return rng.random() < enchant_success_chance(current_level)


def effective_atk(sword_id: str, enchant_level: int) -> int:
    return ITEMS[sword_id]["atk"] + ENCHANT_BONUS * enchant_level


def effective_defense(armor_id: str, enchant_level: int) -> int:
    return ITEMS[armor_id]["defense"] + ENCHANT_BONUS * enchant_level


# --- Economy ----------------------------------------------------------------

def daily_reward(level: int, multiplier: float) -> dict:
    return {"coins": max(1, round((150 + 25 * level) * multiplier)), "item": LIFE_POTION}


def coinflip(bet: int, balance: int, rng) -> dict:
    """Fair 50/50 flip; the wager is clamped to the player's balance."""
    wager = min(bet, balance)
    won = rng.random() < 0.5
    return {"wager": wager, "won": won, "delta": wager if won else -wager}


def sell_price(item_id: str) -> int:
    item = ITEMS[item_id]
    if "sell" in item:
        return item["sell"]
    price = item.get("price")
    return int(price * SELL_RATIO) if price else 0


def best_gear(kind: str, owned_item_ids) -> str:
    """Best owned item of `kind` by stat, falling back to the bare default."""
    default = DEFAULT_SWORD if kind == "sword" else DEFAULT_ARMOR
    stat_key = "atk" if kind == "sword" else "defense"
    best, best_stat = default, ITEMS[default][stat_key]
    for item_id in owned_item_ids:
        item = ITEMS.get(item_id)
        if item and item["kind"] == kind and item[stat_key] > best_stat:
            best, best_stat = item_id, item[stat_key]
    return best


def _normalize(query: str) -> str:
    return str(query or "").strip().lower().replace(" ", "_").replace("-", "_")


def find_item(query: str) -> str | None:
    """Resolve user input to an item id (case/space/underscore-insensitive)."""
    needle = _normalize(query)
    if not needle:
        return None
    for item_id, item in ITEMS.items():
        if needle == item_id or needle == _normalize(item["name"]):
            return item_id
    return None


def find_recipe(query: str) -> str | None:
    item_id = find_item(query)
    return item_id if item_id in RECIPES else None


def find_dungeon(query: str) -> str | None:
    needle = _normalize(query)
    if not needle:
        return None
    for key, dungeon in DUNGEONS.items():
        names = (key, _normalize(dungeon["name"]), _normalize(dungeon["name"]).removeprefix("the_"),
                 _normalize(dungeon["boss"]), _normalize(dungeon["boss"]).removeprefix("the_"))
        if needle in names:
            return key
    return None


def pick_dungeon(level: int, query: str | None = None) -> str | None:
    """Explicit pick by name, else the hardest dungeon the level qualifies for."""
    if query:
        return find_dungeon(query)
    best = None
    for key, dungeon in DUNGEONS.items():
        if level >= dungeon["min_level"]:
            if best is None or dungeon["min_level"] > DUNGEONS[best]["min_level"]:
                best = key
    return best


def shop_pages() -> list[tuple[str, list[str]]]:
    """Ordered shop pages of purchasable item ids, grouped by kind."""
    def of_kind(kind):
        return [i for i, it in ITEMS.items() if it["kind"] == kind and it["price"]]
    return [
        ("Swords", of_kind("sword")),
        ("Armor", of_kind("armor")),
        ("Potions", of_kind("consumable")),
        ("Curios", of_kind("lootbox")),
    ]


# --- Presentation helpers (pure) --------------------------------------------

def progress_bar(value: int, total: int, width: int = 10) -> str:
    if total <= 0:
        return "░" * width
    filled = max(0, min(width, round(width * value / total)))
    return "█" * filled + "░" * (width - filled)


def fmt_duration(seconds: int) -> str:
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}s"
    minutes, secs = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m {secs:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes:02d}m"
