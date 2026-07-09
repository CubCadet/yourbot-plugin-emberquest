"""EmberQuest test suite.

MockContext's ctx.sql is a pure recorder in SDK 0.6.1 (query -> [], scalar ->
None), so functional persistence tests run against FakeSql: an in-memory
sqlite3 shim that translates %s placeholders and records calls in the same
.executed shape. Our SQL deliberately stays inside the sqlite∩postgres
dialect (verified: qualified upserts work on both).

Time is driven by MockClock (shared with ctx.ephemeral/kv) plus a
monkeypatched handlers._now, and all combat RNG goes through handlers._rng,
which tests replace with a scripted stub.
"""

from __future__ import annotations

import json
import random
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest
from yourbot_sdk import CapabilityError
from yourbot_sdk.testing import MockClock, MockContext, make_event

import game
import handlers

ROOT = Path(__file__).resolve().parent.parent
START_EPOCH = 1_000_000.0
UID = "200"


class FakeSql:
    """sqlite3-backed stand-in for ctx.sql with the recorder shape of _MockSql."""

    def __init__(self, conn):
        self._conn = conn
        self.executed = []

    @staticmethod
    def _translate(sql):
        return sql.replace("%s", "?")

    def execute(self, sql, params=None):
        self.executed.append({"sql": sql, "params": params})
        translated = self._translate(sql)
        # sqlite has no ADD COLUMN IF NOT EXISTS (Postgres does); emulate it.
        if "ADD COLUMN IF NOT EXISTS" in translated:
            try:
                self._conn.execute(translated.replace("ADD COLUMN IF NOT EXISTS", "ADD COLUMN"))
                self._conn.commit()
            except sqlite3.OperationalError as exc:
                if "duplicate column" not in str(exc):
                    raise
            return 0
        try:
            cur = self._conn.execute(translated, params or [])
        except sqlite3.Error as exc:
            # The real transport surfaces EVERY host SQL error as RuntimeError
            # ("RPC error (sql.execute): …") — mirror that contract so the
            # plugin's (SdkError, RuntimeError) walls see the same class.
            raise RuntimeError(f"RPC error (sql.execute): {exc}") from exc
        self._conn.commit()
        return cur.rowcount

    def query(self, sql, params=None, *, limit=1000):
        self.executed.append({"sql": sql, "params": params, "limit": limit})
        try:
            cur = self._conn.execute(self._translate(sql), params or [])
        except sqlite3.Error as exc:
            raise RuntimeError(f"RPC error (sql.query): {exc}") from exc
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchmany(limit)]

    def query_one(self, sql, params=None):
        self.executed.append({"sql": sql, "params": params})
        try:
            cur = self._conn.execute(self._translate(sql), params or [])
        except sqlite3.Error as exc:
            raise RuntimeError(f"RPC error (sql.query_one): {exc}") from exc
        row = cur.fetchone()
        if row is None:
            return None
        return dict(zip([d[0] for d in cur.description], row))

    def scalar(self, sql, params=None):
        row = self.query_one(sql, params)
        return next(iter(row.values()), None) if row else None


class ScriptRng:
    """Deterministic stand-in for handlers._rng.

    random() returns a fixed value (0.0 = always win + best drop, 0.99 = always
    lose), randint() returns the lower bound, choice() the first element.
    """

    def __init__(self, random_value=0.0):
        self.random_value = random_value

    def random(self):
        return self.random_value

    def randint(self, a, b):
        return a

    def choice(self, seq):
        return seq[0]


@pytest.fixture
def world(monkeypatch):
    conn = sqlite3.connect(":memory:")
    clock = MockClock(start=START_EPOCH)
    # bootstrap caches are module-level (keyed by server id in production);
    # each test world is a fresh database, so they must start empty
    handlers._schema_ok.clear()
    handlers._schema_retry.clear()

    def make_ctx(capabilities=None):
        ctx = MockContext(clock=clock, capabilities=capabilities)
        ctx.sql = FakeSql(conn)
        return ctx

    ctx = make_ctx()
    handlers.on_install(ctx)
    monkeypatch.setattr(handlers, "_now", lambda: int(clock.now()))
    monkeypatch.setattr(handlers, "_rng", ScriptRng(0.0))
    return SimpleNamespace(conn=conn, clock=clock, ctx=ctx, make_ctx=make_ctx)


def slash(command, uid=UID, name="Tess", channel="1", options=None, **extra):
    kwargs = dict(command_name=command, user_id=uid, user_name=name, channel_id=channel)
    if options is not None:
        kwargs["options"] = options
    kwargs.update(extra)
    return make_event("interaction_create", **kwargs)


def press(custom_id, uid=UID, channel="1"):
    return make_event("interaction_create", interaction_type=3, custom_id=custom_id,
                      user_id=uid, user_name="Tess", channel_id=channel)


def last(ctx):
    return ctx.interaction.responses[-1]


def text_of(response):
    parts = [response.get("content") or ""]
    for embed in response.get("embeds") or []:
        parts.append(embed.get("title", ""))
        parts.append(embed.get("description", ""))
        for field in embed.get("fields") or []:
            parts.append(field.get("name", "") + " " + field.get("value", ""))
        parts.append((embed.get("footer") or {}).get("text", ""))
    return "\n".join(parts)


def get_player(world, uid=UID):
    cur = world.conn.execute("SELECT * FROM players WHERE user_id = ?", [uid])
    row = cur.fetchone()
    return dict(zip([d[0] for d in cur.description], row)) if row else None


def set_player(world, uid=UID, **values):
    sets = ", ".join(f"{k} = ?" for k in values)
    world.conn.execute(f"UPDATE players SET {sets} WHERE user_id = ?", [*values.values(), uid])
    world.conn.commit()


def item_qty(world, item_id, uid=UID):
    row = world.conn.execute(
        "SELECT qty FROM inventory WHERE user_id = ? AND item_id = ?", [uid, item_id]
    ).fetchone()
    return row[0] if row else 0


def started(world, uid=UID):
    handlers.cmd_start(world.ctx, slash("start", uid=uid))
    return world.ctx


def ready_hero(world, uid, level=3, name=None):
    """A started player at the given level (dungeon-qualified by default)."""
    handlers.cmd_start(world.ctx, slash("start", uid=uid, name=name or f"Hero{uid}"))
    set_player(world, uid=uid, level=level)


def give_items(world, uid, **items):
    for item_id, qty in items.items():
        world.conn.execute(
            "INSERT INTO inventory (user_id, item_id, qty) VALUES (?, ?, ?) "
            "ON CONFLICT (user_id, item_id) DO UPDATE SET qty = inventory.qty + ?",
            [uid, item_id, qty, qty])
    world.conn.commit()


# --- game.py pure logic -------------------------------------------------------

def test_xp_curve_monotonic_and_roundtrips():
    assert game.xp_for_level(1) == 0
    previous = -1
    for level in range(1, 40):
        threshold = game.xp_for_level(level)
        assert threshold > previous
        previous = threshold
        assert game.level_from_xp(threshold) == level
        assert game.level_from_xp(threshold + game.xp_for_level(level + 1) - threshold - 1) == level


def test_max_hp_and_progress():
    assert game.max_hp_for_level(1) == 100
    assert game.max_hp_for_level(5) == 140
    in_level, span = game.xp_progress(2, 150)
    assert (in_level, span) == (50, 200)


def test_regen_caps_and_rates():
    assert game.regen_hp(50, 100, 1000, 1000 + 240) == 52
    assert game.regen_hp(99, 100, 1000, 1000 + 100_000) == 100
    assert game.regen_hp(50, 100, 0, 99999) == 50          # never-acted guard
    assert game.regen_hp(120, 100, 1000, 1001) == 100      # over-cap clamps


def test_resolve_hunt_bounds_seeded():
    rng = random.Random(42)
    for _ in range(500):
        result = game.resolve_hunt(5, 7, 2, 80, 1000, rng, 1.0)
        assert result["hp_after"] >= 1
        assert result["damage"] >= 0
        if result["defeated"]:
            assert result["coins_delta"] == -100 and result["xp_gain"] == 0
        elif result["win"]:
            tier = result["foe"]["tier"]
            assert 10 * tier <= result["coins_delta"] <= 18 * tier
            assert 10 * tier <= result["xp_gain"] <= 16 * tier
        else:
            assert result["coins_delta"] == 0 and result["xp_gain"] == 0


def test_resolve_adventure_drop_only_on_win():
    rng = random.Random(7)
    for _ in range(300):
        result = game.resolve_adventure(10, 12, 5, 120, 500, rng, 1.0)
        if result["drop"] is not None:
            assert result["win"] and not result["defeated"]
            assert result["drop"] in (game.LIFE_POTION, game.GREATER_LIFE_POTION)


def test_coinflip_clamps_and_settles():
    assert game.coinflip(5000, 100, ScriptRng(0.0)) == {"wager": 100, "won": True, "delta": 100}
    assert game.coinflip(50, 100, ScriptRng(0.9)) == {"wager": 50, "won": False, "delta": -50}


def test_find_item_and_sell_price():
    assert game.find_item("Cinder Saber") == "cinder_saber"
    assert game.find_item("  CHARSTICK ") == "charstick"
    assert game.find_item("emberbloom-tonic") == game.LIFE_POTION
    assert game.find_item("excalibur") is None
    assert game.find_item("") is None
    assert game.sell_price("charstick") == 75
    assert game.sell_price("fists") == 0


def test_best_gear_picks_strongest_owned():
    assert game.best_gear("sword", ["charstick", "cinder_saber", "padded_vest"]) == "cinder_saber"
    assert game.best_gear("sword", []) == game.DEFAULT_SWORD
    assert game.best_gear("armor", ["padded_vest", game.LIFE_POTION]) == "padded_vest"


def test_progress_bar_and_duration():
    assert game.progress_bar(5, 10) == "█████░░░░░"
    assert game.progress_bar(0, 0) == "░░░░░░░░░░"
    assert game.progress_bar(20, 10) == "██████████"
    assert game.fmt_duration(59) == "59s"
    assert game.fmt_duration(61) == "1m 01s"
    assert game.fmt_duration(3660) == "1h 01m"


# --- Lifecycle ------------------------------------------------------------------

def test_install_creates_schema_and_seeds_settings(world):
    tables = {r[0] for r in world.conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"players", "inventory"} <= tables
    assert world.ctx.kv.get(handlers.KV_MULTIPLIER) == 1.0
    assert world.ctx.kv.get(handlers.KV_CHANNEL) == ""
    handlers.on_ready(world.ctx)  # idempotent re-bootstrap must not raise


def test_install_preserves_existing_settings(world):
    world.ctx.kv.set(handlers.KV_MULTIPLIER, 3.0)
    handlers.on_install(world.ctx)
    assert world.ctx.kv.get(handlers.KV_MULTIPLIER) == 3.0


# --- /start & /profile ------------------------------------------------------------

def test_start_creates_player_idempotently(world):
    started(world)
    player = get_player(world)
    assert player["username"] == "Tess" and player["level"] == 1 and player["coins"] == 0
    assert last(world.ctx)["ephemeral"] is True
    handlers.cmd_start(world.ctx, slash("start"))
    assert world.conn.execute("SELECT COUNT(*) FROM players").fetchone()[0] == 1
    assert "already" in text_of(last(world.ctx))


def test_profile_renders_bars_and_gear(world):
    started(world)
    handlers.cmd_profile(world.ctx, slash("profile"))
    body = text_of(last(world.ctx))
    assert "Tess — Level 1" in body
    assert "█" in body or "░" in body
    assert "Fists" in body and "Traveler's Cloth" in body
    assert last(world.ctx)["embeds"][0]["color"] == handlers.EMBED_COLOR


def test_profile_unregistered_target(world):
    started(world)
    handlers.cmd_profile(world.ctx, slash("profile", options=[{"name": "user", "value": "404"}]))
    assert "hasn't entered" in text_of(last(world.ctx))


def test_profile_requires_start_for_self(world):
    handlers.cmd_profile(world.ctx, slash("profile"))
    assert "/start" in text_of(last(world.ctx))


# --- /hunt --------------------------------------------------------------------------

def test_hunt_win_rewards_and_buttons(world):
    started(world)
    handlers.cmd_hunt(world.ctx, slash("hunt"))
    player = get_player(world)
    assert player["coins"] == 10 and player["xp"] == 10 and player["hp"] == 98
    assert player["last_hunt_at"] == int(world.clock.now())
    response = last(world.ctx)
    assert "Cinder Slime" in text_of(response)
    row = response["components"][0].to_dict() if hasattr(response["components"][0], "to_dict") \
        else response["components"][0]
    ids = [c["custom_id"] for c in row["components"]]
    assert f"{handlers.HUNT_AGAIN_PREFIX}{UID}" in ids
    metrics = [m for m in world.ctx.metrics.recorded if m["metric"] == "commands"]
    assert metrics and metrics[-1]["tags"] == {"command": "hunt"}


def test_hunt_loss_no_rewards(world, monkeypatch):
    monkeypatch.setattr(handlers, "_rng", ScriptRng(0.99))
    started(world)
    handlers.cmd_hunt(world.ctx, slash("hunt"))
    player = get_player(world)
    assert player["coins"] == 0 and player["xp"] == 0
    assert player["hp"] == 94  # randint lower bound 6 × tier 1 − 0 defense
    assert "drove you off" in text_of(last(world.ctx))


def test_hunt_defeat_floors_hp_and_taxes_coins(world):
    started(world)
    set_player(world, hp=3, coins=100, last_action_at=int(world.clock.now()))
    monkey_rng = ScriptRng(0.99)  # loss: damage 6 ≥ hp 3 → knocked out
    handlers._rng = monkey_rng
    handlers.cmd_hunt(world.ctx, slash("hunt"))
    player = get_player(world)
    assert player["hp"] == 1 and player["coins"] == 90
    assert "Knocked out" in text_of(last(world.ctx))


def test_hunt_cooldown_blocks_then_allows(world):
    started(world)
    handlers.cmd_hunt(world.ctx, slash("hunt"))
    handlers.cmd_hunt(world.ctx, slash("hunt"))
    blocked = last(world.ctx)
    assert blocked["ephemeral"] is True and "ready in" in text_of(blocked)
    assert get_player(world)["coins"] == 10  # unchanged by the blocked attempt
    world.clock.advance(game.HUNT_COOLDOWN + 1)
    handlers.cmd_hunt(world.ctx, slash("hunt"))
    assert get_player(world)["coins"] == 20


def test_hunt_cooldown_survives_ephemeral_eviction(world):
    started(world)
    handlers.cmd_hunt(world.ctx, slash("hunt"))
    evicted = world.make_ctx()  # fresh ephemeral store, same database
    handlers.cmd_hunt(evicted, slash("hunt"))
    assert "ready in" in text_of(last(evicted))
    assert get_player(world)["coins"] == 10


def test_hunt_same_instant_double_event_claims_once(world):
    # Two dispatch threads, same instant, no shared ephemeral: the atomic SQL
    # claim must let exactly one hunt through.
    started(world)
    first, second = world.make_ctx(), world.make_ctx()
    handlers.cmd_hunt(first, slash("hunt"))
    handlers.cmd_hunt(second, slash("hunt"))
    assert get_player(world)["coins"] == 10  # one reward, not two
    assert "ready in" in text_of(last(second))


def test_regen_banks_partial_progress(world):
    # Hunting every ~60s must still accrue 1 HP / 120s — the anchor only
    # advances by consumed regen time, never resets outright.
    started(world)
    t0 = int(world.clock.now())
    set_player(world, hp=50, last_action_at=t0)
    world.clock.advance(61)
    handlers.cmd_hunt(world.ctx, slash("hunt"))
    player = get_player(world)
    assert player["hp"] == 48 and player["last_action_at"] == t0  # no tick yet, anchor banked
    world.clock.advance(61)
    handlers.cmd_hunt(world.ctx, slash("hunt"))
    player = get_player(world)
    assert player["hp"] == 47  # 48 + 1 banked regen tick − 2 damage
    assert player["last_action_at"] == t0 + 120


def test_levelup_full_heals_and_announces(world):
    started(world)
    set_player(world, xp=95, hp=50, last_action_at=int(world.clock.now()))
    handlers.cmd_hunt(world.ctx, slash("hunt"))
    player = get_player(world)
    assert player["level"] == 2 and player["xp"] == 105
    assert player["max_hp"] == 110 and player["hp"] == 110
    assert "LEVEL UP" in text_of(last(world.ctx))


def test_low_hp_response_offers_heal_button(world):
    started(world)
    set_player(world, hp=20, last_action_at=int(world.clock.now()))
    handlers.cmd_hunt(world.ctx, slash("hunt"))
    row = last(world.ctx)["components"][0].to_dict()
    ids = [c["custom_id"] for c in row["components"]]
    assert f"{handlers.HEAL_PREFIX}{UID}" in ids


# --- Hunt again / Heal buttons (owner scoping) ---------------------------------------

def test_hunt_again_button_owner_runs(world):
    started(world)
    handlers.cmd_hunt(world.ctx, slash("hunt"))
    world.clock.advance(game.HUNT_COOLDOWN + 1)
    handlers.comp_hunt_again(world.ctx, press(f"{handlers.HUNT_AGAIN_PREFIX}{UID}"))
    assert get_player(world)["coins"] == 20


def test_hunt_again_button_rejects_non_owner(world):
    started(world)
    handlers.cmd_hunt(world.ctx, slash("hunt"))
    world.clock.advance(game.HUNT_COOLDOWN + 1)
    handlers.comp_hunt_again(world.ctx, press(f"{handlers.HUNT_AGAIN_PREFIX}{UID}", uid="999"))
    rejection = last(world.ctx)
    assert rejection["ephemeral"] is True and "isn't your hunt" in text_of(rejection)
    assert get_player(world)["coins"] == 10  # no second hunt happened


def test_heal_button_owner_scoped(world):
    started(world)
    set_player(world, hp=20, coins=100, last_action_at=int(world.clock.now()))
    handlers.comp_heal(world.ctx, press(f"{handlers.HEAL_PREFIX}{UID}", uid="999"))
    assert "isn't your satchel" in text_of(last(world.ctx))
    handlers.comp_heal(world.ctx, press(f"{handlers.HEAL_PREFIX}{UID}"))
    assert get_player(world)["hp"] == 70 and get_player(world)["coins"] == 20


# --- /adventure -----------------------------------------------------------------------

def test_adventure_rewards_drop_and_cooldown(world):
    started(world)
    handlers.cmd_adventure(world.ctx, slash("adventure"))
    player = get_player(world)
    assert player["coins"] == 60 and player["xp"] == 55 and player["hp"] == 95
    assert item_qty(world, game.GREATER_LIFE_POTION) == 1  # roll 0.0 < 0.05
    assert "Smoldering Fen" in text_of(last(world.ctx))

    handlers.cmd_adventure(world.ctx, slash("adventure"))
    assert "ready in" in text_of(last(world.ctx))
    restarted = world.make_ctx()  # durable: SQL timestamp, not ephemeral
    handlers.cmd_adventure(restarted, slash("adventure"))
    assert "ready in" in text_of(last(restarted))
    world.clock.advance(game.ADVENTURE_COOLDOWN + 1)
    handlers.cmd_adventure(world.ctx, slash("adventure"))
    assert get_player(world)["coins"] == 120


# --- /heal ------------------------------------------------------------------------------

def test_heal_consumes_potion_first(world):
    started(world)
    set_player(world, hp=40, coins=100, last_action_at=int(world.clock.now()))
    world.conn.execute("INSERT INTO inventory (user_id, item_id, qty) VALUES (?, ?, 1)",
                       [UID, game.LIFE_POTION])
    world.conn.commit()
    handlers.cmd_heal(world.ctx, slash("heal"))
    player = get_player(world)
    assert player["hp"] == 90 and player["coins"] == 100
    assert item_qty(world, game.LIFE_POTION) == 0


def test_heal_uses_greater_potion_for_big_deficit(world):
    started(world)
    set_player(world, hp=10, last_action_at=int(world.clock.now()))
    world.conn.execute("INSERT INTO inventory (user_id, item_id, qty) VALUES (?, ?, 1)",
                       [UID, game.GREATER_LIFE_POTION])
    world.conn.commit()
    handlers.cmd_heal(world.ctx, slash("heal"))
    assert get_player(world)["hp"] == 100
    assert item_qty(world, game.GREATER_LIFE_POTION) == 0


def test_heal_preserves_greater_potion_on_small_deficit(world):
    # A 250-Ember rare drop must not be burned on a 40 HP scratch while the
    # 80-Ember tonic purchase is available.
    started(world)
    set_player(world, hp=60, coins=100, last_action_at=int(world.clock.now()))
    world.conn.execute("INSERT INTO inventory (user_id, item_id, qty) VALUES (?, ?, 1)",
                       [UID, game.GREATER_LIFE_POTION])
    world.conn.commit()
    handlers.cmd_heal(world.ctx, slash("heal"))
    player = get_player(world)
    assert player["hp"] == 100 and player["coins"] == 20
    assert item_qty(world, game.GREATER_LIFE_POTION) == 1


def test_heal_greater_potion_is_last_resort_when_broke(world):
    started(world)
    set_player(world, hp=95, coins=0, last_action_at=int(world.clock.now()))
    world.conn.execute("INSERT INTO inventory (user_id, item_id, qty) VALUES (?, ?, 1)",
                       [UID, game.GREATER_LIFE_POTION])
    world.conn.commit()
    handlers.cmd_heal(world.ctx, slash("heal"))
    assert get_player(world)["hp"] == 100
    assert item_qty(world, game.GREATER_LIFE_POTION) == 0


def test_heal_never_drives_inventory_negative(world):
    started(world)
    set_player(world, hp=40, coins=0, last_action_at=int(world.clock.now()))
    world.conn.execute("INSERT INTO inventory (user_id, item_id, qty) VALUES (?, ?, 1)",
                       [UID, game.LIFE_POTION])
    world.conn.commit()
    handlers.cmd_heal(world.ctx, slash("heal"))
    assert get_player(world)["hp"] == 90
    handlers.cmd_heal(world.ctx, slash("heal"))  # nothing left to heal with
    assert "costs" in text_of(last(world.ctx))
    assert item_qty(world, game.LIFE_POTION) == 0  # guarded take: never −1


def test_heal_autobuys_when_no_potion(world):
    started(world)
    set_player(world, hp=40, coins=100, last_action_at=int(world.clock.now()))
    handlers.cmd_heal(world.ctx, slash("heal"))
    player = get_player(world)
    assert player["hp"] == 90 and player["coins"] == 20


def test_heal_rejects_broke_and_full(world):
    started(world)
    set_player(world, hp=40, coins=10, last_action_at=int(world.clock.now()))
    handlers.cmd_heal(world.ctx, slash("heal"))
    assert "costs" in text_of(last(world.ctx))
    assert get_player(world)["hp"] == 40 and get_player(world)["coins"] == 10
    set_player(world, hp=100)
    handlers.cmd_heal(world.ctx, slash("heal"))
    assert "full health" in text_of(last(world.ctx))


# --- /inventory, /shop, /buy, /sell ------------------------------------------------------

def test_inventory_lists_items_ephemerally(world):
    started(world)
    world.conn.execute("INSERT INTO inventory (user_id, item_id, qty) VALUES (?, ?, 3)",
                       [UID, game.LIFE_POTION])
    world.conn.commit()
    handlers.cmd_inventory(world.ctx, slash("inventory"))
    response = last(world.ctx)
    assert response["ephemeral"] is True
    assert "Emberbloom Tonic** × 3" in text_of(response)


def test_shop_pages_and_owner_scoped_buttons(world):
    handlers.cmd_shop(world.ctx, slash("shop"))
    first = last(world.ctx)
    assert first["ephemeral"] is True and "Swords" in text_of(first)
    handlers.comp_shop_page(world.ctx, press(f"{handlers.SHOP_PAGE_PREFIX}1:{UID}"))
    assert "Armor" in text_of(last(world.ctx))
    handlers.comp_shop_page(world.ctx, press(f"{handlers.SHOP_PAGE_PREFIX}99:{UID}"))
    assert "Curios" in text_of(last(world.ctx))  # page index clamps to the last page
    handlers.comp_shop_page(world.ctx, press(f"{handlers.SHOP_PAGE_PREFIX}1:{UID}", uid="999"))
    assert "your own catalog" in text_of(last(world.ctx))


def test_buy_deducts_equips_and_stocks(world):
    started(world)
    set_player(world, coins=200)
    handlers.cmd_buy(world.ctx, slash("buy", options=[{"name": "item", "value": "Charstick"}]))
    player = get_player(world)
    assert player["coins"] == 50 and player["sword"] == "charstick"
    assert item_qty(world, "charstick") == 1
    assert "equip it immediately" in text_of(last(world.ctx))


def test_buy_rejects_unknown_and_unaffordable(world):
    started(world)
    handlers.cmd_buy(world.ctx, slash("buy", options=[{"name": "item", "value": "excalibur"}]))
    assert "doesn't stock" in text_of(last(world.ctx))
    handlers.cmd_buy(world.ctx, slash("buy", options=[{"name": "item", "value": "charstick"}]))
    assert "short on Embers" in text_of(last(world.ctx))
    assert get_player(world)["coins"] == 0
    assert item_qty(world, "charstick") == 0  # debit-before-grant: no item minted


def test_buy_does_not_equip_downgrade(world):
    started(world)
    set_player(world, coins=10000, sword="cinder_saber")
    handlers.cmd_buy(world.ctx, slash("buy", options=[{"name": "item", "value": "charstick"}]))
    assert get_player(world)["sword"] == "cinder_saber"


def test_sell_credits_and_unequips_at_zero(world):
    started(world)
    set_player(world, coins=200)
    handlers.cmd_buy(world.ctx, slash("buy", options=[{"name": "item", "value": "charstick"}]))
    handlers.cmd_sell(world.ctx, slash("sell", options=[{"name": "item", "value": "charstick"}]))
    player = get_player(world)
    assert player["coins"] == 50 + 75
    assert player["sword"] == game.DEFAULT_SWORD
    assert item_qty(world, "charstick") == 0
    handlers.cmd_sell(world.ctx, slash("sell", options=[{"name": "item", "value": "charstick"}]))
    assert "don't own" in text_of(last(world.ctx))
    assert item_qty(world, "charstick") == 0  # guarded take: never −1


def test_sell_equipped_falls_back_to_best_owned_gear(world):
    started(world)
    set_player(world, coins=10000)
    handlers.cmd_buy(world.ctx, slash("buy", options=[{"name": "item", "value": "charstick"}]))
    handlers.cmd_buy(world.ctx, slash("buy", options=[{"name": "item", "value": "cinder saber"}]))
    assert get_player(world)["sword"] == "cinder_saber"
    handlers.cmd_sell(world.ctx, slash("sell", options=[{"name": "item", "value": "cinder saber"}]))
    player = get_player(world)
    assert player["sword"] == "charstick"  # owned gear, not bare fists
    assert "fall back" in text_of(last(world.ctx))


# --- /daily -------------------------------------------------------------------------------

def test_daily_claims_blocks_and_survives_restart(world):
    started(world)
    handlers.cmd_daily(world.ctx, slash("daily"))
    assert get_player(world)["coins"] == 175  # (150 + 25·level1) × 1.0
    assert item_qty(world, game.LIFE_POTION) == 1

    handlers.cmd_daily(world.ctx, slash("daily"))
    assert "renews in" in text_of(last(world.ctx))
    assert get_player(world)["coins"] == 175

    # Restart: fresh worker = fresh ephemeral, durable SQL timestamp still gates.
    restarted = world.make_ctx()
    handlers.cmd_daily(restarted, slash("daily"))
    assert "renews in" in text_of(last(restarted))
    assert get_player(world)["coins"] == 175

    world.clock.advance(game.DAILY_COOLDOWN + 1)
    handlers.cmd_daily(restarted, slash("daily"))
    assert get_player(world)["coins"] == 350
    assert item_qty(world, game.LIFE_POTION) == 2


def test_daily_same_instant_double_event_grants_once(world):
    started(world)
    first, second = world.make_ctx(), world.make_ctx()
    handlers.cmd_daily(first, slash("daily"))
    handlers.cmd_daily(second, slash("daily"))  # same instant, fresh ephemeral
    assert get_player(world)["coins"] == 175
    assert item_qty(world, game.LIFE_POTION) == 1
    assert "renews in" in text_of(last(second))


def test_daily_scales_with_economy_multiplier(world):
    world.ctx.kv.set(handlers.KV_MULTIPLIER, 2.0)
    started(world)
    handlers.cmd_daily(world.ctx, slash("daily"))
    assert get_player(world)["coins"] == 350


# --- /top ------------------------------------------------------------------------------------

def _seed_heroes(world):
    now = int(world.clock.now())
    rows = [
        ("1", "Aria", 5, game.xp_for_level(5) + 10, 50),
        ("2", "Borin", 7, game.xp_for_level(7), 10),
        ("3", "Cinder", 5, game.xp_for_level(5) + 99, 900),
    ]
    for uid, name, level, xp, coins in rows:
        world.conn.execute(
            "INSERT INTO players (user_id, username, level, xp, coins, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)", [uid, name, level, xp, coins, now])
    world.conn.commit()


def test_leaderboard_by_level_breaks_ties_on_xp(world):
    _seed_heroes(world)
    handlers.cmd_top(world.ctx, slash("top"))
    body = text_of(last(world.ctx))
    assert body.index("**Borin**") < body.index("**Cinder**") < body.index("**Aria**")
    assert "🥇" in body


def test_leaderboard_by_coins_and_empty(world):
    _seed_heroes(world)
    handlers.cmd_top(world.ctx, slash("top",
                                              options=[{"name": "metric", "value": "coins"}]))
    body = text_of(last(world.ctx))
    assert body.index("**Cinder**") < body.index("**Aria**") < body.index("**Borin**")

    empty = world.make_ctx()
    world.conn.execute("DELETE FROM players")
    world.conn.commit()
    handlers.cmd_top(empty, slash("top"))
    assert "No heroes" in text_of(last(empty))


# --- /coinflip ---------------------------------------------------------------------------------

def test_coinflip_clamps_bet_and_pays_win(world):
    started(world)
    set_player(world, coins=100)
    handlers.cmd_coinflip(world.ctx, slash("coinflip", options=[{"name": "bet", "value": 5000}]))
    assert get_player(world)["coins"] == 200
    body = text_of(last(world.ctx))
    assert "clamped" in body.lower() and "no real-world value" in body
    wagers = [m for m in world.ctx.metrics.recorded if m["metric"] == "coinflip_wagered"]
    assert wagers and wagers[0]["value"] == 100.0


def test_coinflip_loss_and_guards(world, monkeypatch):
    monkeypatch.setattr(handlers, "_rng", ScriptRng(0.9))
    started(world)
    set_player(world, coins=100)
    handlers.cmd_coinflip(world.ctx, slash("coinflip", options=[{"name": "bet", "value": 40}]))
    assert get_player(world)["coins"] == 60

    handlers.cmd_coinflip(world.ctx, slash("coinflip", options=[{"name": "bet", "value": 5}]))
    assert "Minimum wager" in text_of(last(world.ctx))

    set_player(world, coins=5)  # below MIN_BET: no sub-minimum effective wagers
    handlers.cmd_coinflip(world.ctx, slash("coinflip", options=[{"name": "bet", "value": 50}]))
    assert "need at least" in text_of(last(world.ctx))
    assert get_player(world)["coins"] == 5


# --- Server settings ------------------------------------------------------------------------------

def test_allowed_channel_restricts_commands(world):
    world.ctx.kv.set(handlers.KV_CHANNEL, "42")
    handlers.cmd_start(world.ctx, slash("start", channel="1"))
    assert "<#42>" in text_of(last(world.ctx))
    assert get_player(world) is None
    handlers.cmd_start(world.ctx, slash("start", channel="42"))
    assert get_player(world) is not None


# --- Dashboard handlers ------------------------------------------------------------------------------

def test_dashboard_stat_cards(world):
    _seed_heroes(world)
    params = {"discord_srv_id": "999"}
    assert handlers.dash_player_count(world.ctx, params) == {"value": 3, "change": ""}
    assert handlers.dash_economy_total(world.ctx, params) == {"value": 960, "change": ""}
    today = handlers.dash_commands_today(world.ctx, params)
    assert today == {"value": 0, "change": ""}  # mock metrics always report zero


def test_dashboard_chart_shape(world):
    chart = handlers.dash_commands_chart(world.ctx, {"discord_srv_id": "999"})
    assert chart["labels"] == []
    assert chart["series"] and "name" in chart["series"][0] and "data" in chart["series"][0]


def test_dashboard_settings_roundtrip(world):
    params = {"discord_srv_id": "999",
              "values": {"economy_multiplier": "2.5", "allowed_channel_id": "<#42>"}}
    assert handlers.dash_save_settings(world.ctx, params) == {"ok": True}
    values = handlers.dash_get_settings(world.ctx, {"discord_srv_id": "999"})["values"]
    assert values == {"economy_multiplier": 2.5, "allowed_channel_id": "42"}

    bad_mult = {"discord_srv_id": "999",
                "values": {"economy_multiplier": "abc", "allowed_channel_id": "42"}}
    assert handlers.dash_save_settings(world.ctx, bad_mult) == {"ok": True}
    assert world.ctx.kv.get(handlers.KV_MULTIPLIER) == 2.5  # unparsable input keeps old value

    bad_channel = {"discord_srv_id": "999",
                   "values": {"economy_multiplier": "1", "allowed_channel_id": "general"}}
    assert "error" in handlers.dash_save_settings(world.ctx, bad_channel)

    clamped = {"discord_srv_id": "999",
               "values": {"economy_multiplier": "99", "allowed_channel_id": ""}}
    assert handlers.dash_save_settings(world.ctx, clamped) == {"ok": True}
    assert world.ctx.kv.get(handlers.KV_MULTIPLIER) == 10.0


# --- Phase 2 game math ---------------------------------------------------------------------------

def test_enchant_cost_and_chance_curves():
    assert game.enchant_cost(1) == {"coins": 100, "materials": 1}
    assert game.enchant_cost(5) == {"coins": 2500, "materials": 5}
    assert game.enchant_success_chance(0) == 0.95
    assert game.enchant_success_chance(4) == pytest.approx(0.35)
    assert game.enchant_success_chance(10) == 0.25  # floored


def test_effective_stats_include_enchant():
    assert game.effective_atk("charstick", 2) == 7
    assert game.effective_atk("fists", 0) == 0
    assert game.effective_defense("padded_vest", 3) == 8


def test_pick_and_find_dungeon():
    assert game.pick_dungeon(2) is None
    assert game.pick_dungeon(3) == "kiln"
    assert game.pick_dungeon(8) == "cinderkeep"
    assert game.pick_dungeon(40) == "crucible"
    assert game.find_dungeon("Smelterdeep") == "kiln"
    assert game.find_dungeon("kilnwarden") == "kiln"
    assert game.find_dungeon("nowhere") is None
    assert game.find_recipe("dawnforged blade") == "dawnforged_blade"
    assert game.find_recipe("charstick") is None  # an item, but not a recipe


def test_resolve_dungeon_invariants_seeded():
    rng = random.Random(11)
    party = [
        {"user_id": "1", "username": "A", "level": 5, "atk": 7, "defense": 2, "hp": 60},
        {"user_id": "2", "username": "B", "level": 8, "atk": 12, "defense": 5, "hp": 3},
        {"user_id": "3", "username": "C", "level": 3, "atk": 0, "defense": 0, "hp": 120},
    ]
    for _ in range(300):
        result = game.resolve_dungeon(party, game.DUNGEONS["cinderkeep"], rng, 1.0)
        for outcome in result["outcomes"]:
            assert outcome["hp_after"] >= 1
            if not result["win"]:
                assert outcome["coins"] == 0 and outcome["xp"] == 0 and not outcome["drops"]
            if outcome["defeated"]:
                assert len(outcome["drops"]) <= 1
            for item_id, count in outcome["drops"]:
                assert item_id in game.ITEMS and count >= 1


def test_material_sell_values_and_recipes_reference_real_items():
    for recipe_id, recipe in game.RECIPES.items():
        assert recipe_id in game.ITEMS
        for material_id in recipe["materials"]:
            assert game.ITEMS[material_id]["kind"] == "material"
    assert game.sell_price("wraithsilk") == 40
    assert game.sell_price("dawnforged_blade") == 4000


# --- Phase 2: schema migration ----------------------------------------------------------------

_PHASE1_PLAYERS_DDL = (
    "CREATE TABLE players (user_id TEXT PRIMARY KEY, username TEXT NOT NULL DEFAULT '',"
    " level INT NOT NULL DEFAULT 1, xp BIGINT NOT NULL DEFAULT 0, hp INT NOT NULL DEFAULT 100,"
    " max_hp INT NOT NULL DEFAULT 100, coins BIGINT NOT NULL DEFAULT 0,"
    " sword TEXT NOT NULL DEFAULT 'fists', armor TEXT NOT NULL DEFAULT 'cloth',"
    " created_at BIGINT NOT NULL, last_action_at BIGINT NOT NULL DEFAULT 0,"
    " last_hunt_at BIGINT NOT NULL DEFAULT 0, last_adventure_at BIGINT NOT NULL DEFAULT 0,"
    " last_daily_at BIGINT NOT NULL DEFAULT 0)"
)


def test_migration_upgrades_phase1_database(world):
    conn = sqlite3.connect(":memory:")
    conn.execute(_PHASE1_PLAYERS_DDL)  # a server that installed 0.1.0
    ctx = MockContext(clock=world.clock)
    ctx.sql = FakeSql(conn)
    handlers.on_ready(ctx)
    columns = {row[1] for row in conn.execute("PRAGMA table_info(players)")}
    assert {"last_dungeon_at", "sword_enchant", "armor_enchant", "last_duel_at",
            "pet", "rekindles"} <= columns
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"dungeons", "dungeon_members", "duels", "trades",
            "guilds", "guild_members", "arena_entries", "arena_days",
            "quest_progress"} <= tables
    handlers.on_ready(ctx)  # idempotent re-run


# --- Phase 2: dungeons -------------------------------------------------------------------------

def open_lobby(world, uid=UID, dungeon_id="d1", level=3):
    ready_hero(world, uid, level=level)
    handlers.cmd_dungeon(world.ctx, slash("dungeon", uid=uid, interaction_id=dungeon_id))
    return dungeon_id


def lobby_row(world, dungeon_id="d1"):
    row = world.conn.execute("SELECT * FROM dungeons WHERE id = ?", [dungeon_id]).fetchone()
    if row is None:
        return None
    cols = [d[0] for d in world.conn.execute("SELECT * FROM dungeons LIMIT 0").description]
    return dict(zip(cols, row))


def test_dungeon_rejects_low_level(world):
    started(world)  # level 1 < kiln's 3
    handlers.cmd_dungeon(world.ctx, slash("dungeon"))
    assert "level 3" in text_of(last(world.ctx))
    assert world.conn.execute("SELECT COUNT(*) FROM dungeons").fetchone()[0] == 0


def test_dungeon_open_creates_lobby_with_open_buttons(world):
    open_lobby(world)
    response = last(world.ctx)
    assert response["ephemeral"] is False or "ephemeral" not in response or not response["ephemeral"]
    row = response["components"][0].to_dict()
    ids = [c["custom_id"] for c in row["components"]]
    assert f"{handlers.DUNGEON_JOIN_PREFIX}d1" in ids
    assert f"{handlers.DUNGEON_BEGIN_PREFIX}d1" in ids
    assert lobby_row(world)["status"] == "open"
    # a second /dungeon shows the same lobby instead of opening another
    ready_hero(world, "300")
    handlers.cmd_dungeon(world.ctx, slash("dungeon", uid="300", interaction_id="d2"))
    assert world.conn.execute("SELECT COUNT(*) FROM dungeons").fetchone()[0] == 1
    assert "1/4" in text_of(last(world.ctx))


def test_dungeon_join_open_to_all_then_dedupes(world):
    open_lobby(world)
    ready_hero(world, "300")
    handlers.comp_dungeon_join(world.ctx, press(f"{handlers.DUNGEON_JOIN_PREFIX}d1", uid="300"))
    confirm = last(world.ctx)
    # A non-final join now edits the shared public lobby card in place, so the
    # roster/count update live for everyone (rather than an ephemeral note).
    assert confirm["update_message"] is True
    assert not confirm.get("ephemeral") and "2/4" in text_of(confirm)
    ids = [c["custom_id"] for c in confirm["components"][0].to_dict()["components"]]
    assert f"{handlers.DUNGEON_JOIN_PREFIX}d1" in ids  # Join / Sound-horn stay live
    broadcasts = world.ctx.discord.messages_sent
    assert broadcasts and "joined the expedition" in broadcasts[-1]["content"]
    handlers.comp_dungeon_join(world.ctx, press(f"{handlers.DUNGEON_JOIN_PREFIX}d1", uid="300"))
    assert "already on the roster" in text_of(last(world.ctx))


def test_dungeon_join_gates_level_start_and_recovery(world):
    open_lobby(world)
    handlers.comp_dungeon_join(world.ctx, press(f"{handlers.DUNGEON_JOIN_PREFIX}d1", uid="999"))
    assert "/start" in text_of(last(world.ctx))
    ready_hero(world, "888", level=1)
    handlers.comp_dungeon_join(world.ctx, press(f"{handlers.DUNGEON_JOIN_PREFIX}d1", uid="888"))
    assert "demands level" in text_of(last(world.ctx))
    ready_hero(world, "777")
    set_player(world, uid="777", last_dungeon_at=int(world.clock.now()))
    handlers.comp_dungeon_join(world.ctx, press(f"{handlers.DUNGEON_JOIN_PREFIX}d1", uid="777"))
    assert "recovering" in text_of(last(world.ctx))


def test_dungeon_begin_requires_membership_and_min_party(world):
    open_lobby(world)
    ready_hero(world, "300")
    handlers.comp_dungeon_begin(world.ctx, press(f"{handlers.DUNGEON_BEGIN_PREFIX}d1", uid="300"))
    assert "Only party members" in text_of(last(world.ctx))
    handlers.comp_dungeon_begin(world.ctx, press(f"{handlers.DUNGEON_BEGIN_PREFIX}d1", uid=UID))
    assert "at least" in text_of(last(world.ctx))
    assert lobby_row(world)["status"] == "open"


def test_dungeon_begin_resolves_rewards_all_members(world):
    open_lobby(world)
    ready_hero(world, "300")
    handlers.comp_dungeon_join(world.ctx, press(f"{handlers.DUNGEON_JOIN_PREFIX}d1", uid="300"))
    handlers.comp_dungeon_begin(world.ctx, press(f"{handlers.DUNGEON_BEGIN_PREFIX}d1", uid=UID))

    assert world.ctx.interaction.defers and world.ctx.interaction.defers[-1]["ephemeral"] is False
    battle = world.ctx.interaction.followups[-1]
    assert "has fallen" in battle["embeds"][0]["title"]  # ScriptRng(0.0): guaranteed win

    now = int(world.clock.now())
    for uid in (UID, "300"):
        player = get_player(world, uid)
        # win at tier 2: coins 80×2, xp 70×2; damage 3×2 − 0 def = 6
        assert player["coins"] == 160 and player["xp"] == 140
        # level was set to 3 directly (xp 140 < level-3 threshold): the
        # ratchet must never downgrade it to the computed level 2
        assert player["level"] == 3 and player["hp"] == 94
        assert player["last_dungeon_at"] == now
        assert item_qty(world, "ember_shard", uid) == 1   # drop rolls 0.0 → both drop
        assert item_qty(world, "wraithsilk", uid) == 1
    assert lobby_row(world)["status"] == "resolved"
    assert any(m["metric"] == "dungeons_resolved" for m in world.ctx.metrics.recorded)

    handlers.comp_dungeon_begin(world.ctx, press(f"{handlers.DUNGEON_BEGIN_PREFIX}d1", uid=UID))
    assert "already over" in text_of(last(world.ctx))


def test_dungeon_auto_begins_when_party_fills(world):
    open_lobby(world)
    for uid in ("300", "400", "500"):
        ready_hero(world, uid)
        handlers.comp_dungeon_join(world.ctx, press(f"{handlers.DUNGEON_JOIN_PREFIX}d1", uid=uid))
    final_confirm = last(world.ctx)
    assert final_confirm["ephemeral"] is True and "assault" in text_of(final_confirm)
    # resolution was broadcast to the channel, not responded to an interaction
    broadcast_embeds = [m for m in world.ctx.discord.messages_sent if m.get("embeds")]
    assert broadcast_embeds and "has fallen" in broadcast_embeds[-1]["embeds"][0]["title"]
    assert lobby_row(world)["status"] == "resolved"
    for uid in (UID, "300", "400", "500"):
        assert get_player(world, uid)["coins"] > 0
    # a full lobby admits no fifth hero — and the expedition is already over
    ready_hero(world, "600")
    handlers.comp_dungeon_join(world.ctx, press(f"{handlers.DUNGEON_JOIN_PREFIX}d1", uid="600"))
    assert "gone cold" in text_of(last(world.ctx)) or "full" in text_of(last(world.ctx))


def test_dungeon_lobby_expires_lazily(world):
    open_lobby(world)
    world.clock.advance(game.DUNGEON_LOBBY_TTL + 1)
    ready_hero(world, "300")
    handlers.comp_dungeon_join(world.ctx, press(f"{handlers.DUNGEON_JOIN_PREFIX}d1", uid="300"))
    assert "gone cold" in text_of(last(world.ctx))
    assert lobby_row(world)["status"] == "expired"
    # the next /dungeon can raise a fresh lobby
    handlers.cmd_dungeon(world.ctx, slash("dungeon", uid="300", interaction_id="d2"))
    assert lobby_row(world, "d2")["status"] == "open"


def test_dungeon_cooldown_blocks_new_expedition(world):
    started(world)
    set_player(world, level=3, last_dungeon_at=int(world.clock.now()))
    handlers.cmd_dungeon(world.ctx, slash("dungeon", interaction_id="d9"))
    assert "recovering" in text_of(last(world.ctx))
    world.clock.advance(game.DUNGEON_COOLDOWN + 1)
    handlers.cmd_dungeon(world.ctx, slash("dungeon", interaction_id="d9"))
    assert lobby_row(world, "d9") is not None


def test_dungeon_open_race_converges_on_one_lobby(world):
    open_lobby(world)
    ready_hero(world, "300")
    racer = world.make_ctx()  # fresh ephemeral: the 5s open spam-guard is empty
    handlers.cmd_dungeon(racer, slash("dungeon", uid="300", interaction_id="d2"))
    assert world.conn.execute("SELECT COUNT(*) FROM dungeons").fetchone()[0] == 1
    assert "1/4" in text_of(last(racer))  # loser was shown the winner's lobby


def test_one_open_lobby_enforced_by_lock_claim(world):
    # The host forbids CREATE UNIQUE INDEX, so the one-lobby invariant rides
    # the locks table: while a lobby is open its lock is held, and a truly
    # concurrent open (fresh ephemeral, stale read) loses the atomic claim.
    open_lobby(world)
    lock = world.conn.execute("SELECT ref FROM locks WHERE name = 'dungeon'").fetchone()
    assert lock[0] == "d1"
    racer = world.make_ctx()
    assert handlers._acquire_lock(racer, "dungeon", "dungeon", "d-race",
                                  int(world.clock.now())) is False
    assert world.conn.execute("SELECT COUNT(*) FROM dungeons").fetchone()[0] == 1


def test_lock_birth_grace_protects_mid_open_claims(world):
    # An open claims its lock one roundtrip before inserting the row: a young
    # lock with no holder row must NOT be breakable (TOCTOU), only an old one.
    now = int(world.clock.now())
    assert handlers._acquire_lock(world.ctx, "duel", "duel:200", "fresh-ref", now)
    # holder row doesn't exist yet — a racer must lose the claim
    racer = world.make_ctx()
    assert handlers._acquire_lock(racer, "duel", "duel:200", "thief-ref", now) is False
    lock = world.conn.execute("SELECT ref FROM locks WHERE name = 'duel:200'").fetchone()
    assert lock[0] == "fresh-ref"
    # but an orphaned claim (no row ever landed) is breakable after the grace
    world.clock.advance(handlers._LOCK_BIRTH_GRACE + 1)
    assert handlers._acquire_lock(racer, "duel", "duel:200", "heir-ref",
                                  int(world.clock.now())) is True


def test_stuck_resolving_rows_are_recovered_by_sweeps(world):
    started(world)
    ready_hero(world, "300")
    old = int(world.clock.now()) - game.TRADE_TTL - 700
    world.conn.execute(
        "INSERT INTO duels (id, challenger_id, target_id, challenger_name, bet, "
        "status, created_at, epoch) VALUES ('wedged-d', ?, '300', 'Tess', 80, "
        "'resolving', ?, 0)", [UID, old])
    world.conn.execute(
        "INSERT INTO trades (id, seller_id, buyer_id, seller_name, item_id, qty, "
        "price, status, created_at, epoch) VALUES ('wedged-t', ?, '300', 'Tess', "
        "'ember_tonic', 2, 50, 'resolving', ?, 0)", [UID, old])
    world.conn.commit()
    handlers._sweep_stale_duels(world.ctx, int(world.clock.now()))
    handlers._sweep_stale_trades(world.ctx, int(world.clock.now()))
    assert world.conn.execute(
        "SELECT status FROM duels WHERE id = 'wedged-d'").fetchone()[0] == "expired"
    assert world.conn.execute(
        "SELECT status FROM trades WHERE id = 'wedged-t'").fetchone()[0] == "expired"
    assert get_player(world)["coins"] == 80           # escrowed stake came home
    assert item_qty(world, game.LIFE_POTION) == 2     # escrowed goods came home


def test_wedged_resolving_lobby_is_recovered(world):
    open_lobby(world)
    world.conn.execute("UPDATE dungeons SET status = 'resolving' WHERE id = 'd1'")
    world.conn.commit()
    world.clock.advance(game.DUNGEON_LOBBY_TTL + 700)
    ready_hero(world, "300")
    handlers.cmd_dungeon(world.ctx, slash("dungeon", uid="300", interaction_id="d2"))
    assert lobby_row(world)["status"] == "expired"
    assert get_player(world, UID)["last_dungeon_at"] == 0  # 2h claim refunded
    assert lobby_row(world, "d2") is not None              # server unblocked


def test_failed_defer_leaves_lobby_open(world, monkeypatch):
    open_lobby(world)
    ready_hero(world, "300")
    handlers.comp_dungeon_join(world.ctx, press(f"{handlers.DUNGEON_JOIN_PREFIX}d1", uid="300"))

    from yourbot_sdk import SdkError

    def explode(*args, **kwargs):
        raise SdkError("rate limited")

    monkeypatch.setattr(world.ctx.interaction, "defer", explode, raising=False)
    handlers.comp_dungeon_begin(world.ctx, press(f"{handlers.DUNGEON_BEGIN_PREFIX}d1", uid=UID))
    # the defer failed BEFORE the resolution claim: nothing is wedged
    assert lobby_row(world)["status"] == "open"


def test_locks_self_heal_when_holder_is_finished(world):
    # A lock orphaned by a crash (holder row terminal) is broken and re-claimed.
    open_lobby(world)
    world.conn.execute("UPDATE dungeons SET status = 'resolved' WHERE id = 'd1'")
    world.conn.commit()
    assert handlers._acquire_lock(world.ctx, "dungeon", "dungeon", "d2",
                                  int(world.clock.now())) is True
    lock = world.conn.execute("SELECT ref FROM locks WHERE name = 'dungeon'").fetchone()
    assert lock[0] == "d2"


def test_lock_released_at_every_lobby_terminal(world):
    open_lobby(world)
    world.clock.advance(game.DUNGEON_LOBBY_TTL + 1)
    handlers.cmd_dungeon(world.ctx, slash("dungeon", interaction_id="probe"))
    # the stale lobby expired lazily -> its lock must be gone or re-owned
    lock = world.conn.execute("SELECT ref FROM locks WHERE name = 'dungeon'").fetchone()
    assert lock is None or lock[0] != "d1"


def test_dungeon_cooldown_claimed_at_join_and_refunded_on_expiry(world):
    open_lobby(world)  # leader 200's claim lands at open
    now = int(world.clock.now())
    assert get_player(world, UID)["last_dungeon_at"] == now
    ready_hero(world, "300")
    handlers.comp_dungeon_join(world.ctx, press(f"{handlers.DUNGEON_JOIN_PREFIX}d1", uid="300"))
    assert get_player(world, "300")["last_dungeon_at"] == now
    # the lobby goes cold unfought → everyone's 2h claim is given back; 300's
    # very next /dungeon succeeds, immediately re-claiming at the new instant
    world.clock.advance(game.DUNGEON_LOBBY_TTL + 1)
    handlers.cmd_dungeon(world.ctx, slash("dungeon", uid="300", interaction_id="d2"))
    assert get_player(world, UID)["last_dungeon_at"] == 0          # leader: refunded
    assert lobby_row(world, "d2") is not None                      # 300 reopened at once
    assert get_player(world, "300")["last_dungeon_at"] == int(world.clock.now())


def test_dungeon_resolution_failure_never_wedges_lobby(world, monkeypatch):
    open_lobby(world)
    ready_hero(world, "300")
    handlers.comp_dungeon_join(world.ctx, press(f"{handlers.DUNGEON_JOIN_PREFIX}d1", uid="300"))

    def explode(*args, **kwargs):
        raise RuntimeError("RPC error (sql.execute): boom")

    monkeypatch.setattr(game, "resolve_dungeon", explode)
    handlers.comp_dungeon_begin(world.ctx, press(f"{handlers.DUNGEON_BEGIN_PREFIX}d1", uid=UID))
    assert lobby_row(world)["status"] == "resolved"  # never stuck in 'resolving'
    battle = world.ctx.interaction.followups[-1]
    assert "collapses" in battle["embeds"][0]["title"]
    assert any(e["level"] == "error" for e in world.ctx.log_entries)


# --- Phase 2: crafting ---------------------------------------------------------------------------

def test_craft_list_shows_recipes(world):
    started(world)
    handlers.cmd_craft(world.ctx, slash("craft"))
    response = last(world.ctx)
    assert response["ephemeral"] is True
    body = text_of(response)
    for recipe_id in game.RECIPES:
        assert game.ITEMS[recipe_id]["name"] in body
    buttons = response["components"][0].to_dict()["components"]
    assert all(b.get("disabled") for b in buttons)  # no materials yet


def test_craft_consumes_materials_and_fee(world):
    started(world)
    set_player(world, coins=100)
    give_items(world, UID, ember_shard=1)
    handlers.cmd_craft(world.ctx, slash("craft", options=[{"name": "item", "value": "Emberbloom Tonic"}]))
    assert item_qty(world, game.LIFE_POTION) == 1
    assert item_qty(world, "ember_shard") == 0
    assert get_player(world)["coins"] == 80
    assert "Forged" in text_of(last(world.ctx))


def test_craft_shortage_refunds_everything(world):
    started(world)
    set_player(world, coins=200)
    give_items(world, UID, ember_shard=2)  # phoenix draught also needs wraithsilk
    handlers.cmd_craft(world.ctx, slash("craft", options=[{"name": "item", "value": "Phoenix Draught"}]))
    assert "Missing materials" in text_of(last(world.ctx))
    assert item_qty(world, "ember_shard") == 2   # taken shards returned
    assert get_player(world)["coins"] == 200     # fee refunded
    assert item_qty(world, game.GREATER_LIFE_POTION) == 0


def test_craft_rejects_unknown_and_unaffordable_fee(world):
    started(world)
    handlers.cmd_craft(world.ctx, slash("craft", options=[{"name": "item", "value": "excalibur"}]))
    assert "no such recipe" in text_of(last(world.ctx))
    give_items(world, UID, ember_shard=1)  # has the material but not the 20-Ember fee
    handlers.cmd_craft(world.ctx, slash("craft", options=[{"name": "item", "value": "ember tonic"}]))
    assert "forge fee" in text_of(last(world.ctx)).lower()
    assert item_qty(world, "ember_shard") == 1


def test_craft_gear_equips_and_clears_enchant(world):
    started(world)
    set_player(world, coins=3000, sword="ironspark_blade", sword_enchant=3)
    give_items(world, UID, ashsteel_ingot=3, magmaheart=1)
    handlers.cmd_craft(world.ctx, slash("craft", options=[{"name": "item", "value": "dawnforged blade"}]))
    player = get_player(world)
    assert player["sword"] == "dawnforged_blade" and player["sword_enchant"] == 0
    assert player["coins"] == 500
    assert item_qty(world, "dawnforged_blade") == 1
    assert "off the anvil" in text_of(last(world.ctx))


def test_craft_button_is_owner_scoped(world):
    started(world)
    handlers.comp_craft(world.ctx, press(f"{handlers.CRAFT_PREFIX}{game.LIFE_POTION}:{UID}", uid="999"))
    assert "patron" in text_of(last(world.ctx))
    give_items(world, UID, ember_shard=1)
    set_player(world, coins=100)
    handlers.comp_craft(world.ctx, press(f"{handlers.CRAFT_PREFIX}{game.LIFE_POTION}:{UID}", uid=UID))
    assert item_qty(world, game.LIFE_POTION) == 1


# --- Phase 2: enchanting ----------------------------------------------------------------------------

def test_enchant_requires_real_gear_and_valid_slot(world):
    started(world)
    handlers.cmd_enchant(world.ctx, slash("enchant", options=[{"name": "slot", "value": "sword"}]))
    assert "no enchantment" in text_of(last(world.ctx))
    handlers.cmd_enchant(world.ctx, slash("enchant", options=[{"name": "slot", "value": "hat"}]))
    assert "sword** or **armor" in text_of(last(world.ctx))


def test_enchant_success_consumes_and_upgrades(world):
    started(world)
    set_player(world, coins=200, sword="charstick")
    give_items(world, UID, ember_shard=1)
    handlers.cmd_enchant(world.ctx, slash("enchant", options=[{"name": "slot", "value": "sword"}]))
    player = get_player(world)
    assert player["sword_enchant"] == 1          # ScriptRng(0.0) < 0.95 succeeds
    assert player["coins"] == 100 and item_qty(world, "ember_shard") == 0
    assert "+1" in text_of(last(world.ctx))


def test_enchant_failure_keeps_level_but_spends(world, monkeypatch):
    monkeypatch.setattr(handlers, "_rng", ScriptRng(0.99))  # 0.99 ≥ 0.95 fails
    started(world)
    set_player(world, coins=200, sword="charstick")
    give_items(world, UID, ember_shard=1)
    handlers.cmd_enchant(world.ctx, slash("enchant", options=[{"name": "slot", "value": "sword"}]))
    player = get_player(world)
    assert player["sword_enchant"] == 0
    assert player["coins"] == 100 and item_qty(world, "ember_shard") == 0
    assert "sputters" in text_of(last(world.ctx))


def test_enchant_shortages_refund_fee_and_cap_applies(world):
    started(world)
    set_player(world, coins=200, sword="charstick")
    handlers.cmd_enchant(world.ctx, slash("enchant", options=[{"name": "slot", "value": "sword"}]))
    assert "too few" in text_of(last(world.ctx))
    assert get_player(world)["coins"] == 200     # fee refunded on shard shortage
    set_player(world, sword_enchant=game.ENCHANT_MAX)
    handlers.cmd_enchant(world.ctx, slash("enchant", options=[{"name": "slot", "value": "sword"}]))
    assert "no more" in text_of(last(world.ctx))


def test_enchant_shows_in_profile_and_combat_stats(world):
    started(world)
    set_player(world, sword="charstick", sword_enchant=2)
    handlers.cmd_profile(world.ctx, slash("profile"))
    body = text_of(last(world.ctx))
    assert "Charstick +2" in body and "+7 atk" in body


def test_buy_compares_against_effective_stats(world):
    started(world)
    set_player(world, coins=5000, sword="charstick", sword_enchant=3)
    # Charstick +3 is effectively 9 atk; Cinder Saber's base 7 is a DOWNGRADE —
    # buying it must not equip it or destroy the enchant.
    handlers.cmd_buy(world.ctx, slash("buy", options=[{"name": "item", "value": "cinder saber"}]))
    player = get_player(world)
    assert player["sword"] == "charstick" and player["sword_enchant"] == 3
    assert item_qty(world, "cinder_saber") == 1
    # A genuine upgrade (12 > 9) equips and clears the old enchant.
    handlers.cmd_buy(world.ctx, slash("buy", options=[{"name": "item", "value": "ironspark blade"}]))
    player = get_player(world)
    assert player["sword"] == "ironspark_blade" and player["sword_enchant"] == 0


# --- Phase 2: materials economy --------------------------------------------------------------------

def test_materials_sellable_but_not_buyable(world):
    started(world)
    give_items(world, UID, wraithsilk=2)
    handlers.cmd_sell(world.ctx, slash("sell", options=[{"name": "item", "value": "wraithsilk"}]))
    player = get_player(world)
    assert player["coins"] == 40 and item_qty(world, "wraithsilk") == 1
    handlers.cmd_buy(world.ctx, slash("buy", options=[{"name": "item", "value": "wraithsilk"}]))
    assert "doesn't stock" in text_of(last(world.ctx))
    handlers.cmd_buy(world.ctx, slash("buy", options=[{"name": "item", "value": "dawnforged blade"}]))
    assert "doesn't stock" in text_of(last(world.ctx))


def test_hunt_and_adventure_drop_materials_on_lucky_roll(world):
    started(world)
    handlers.cmd_hunt(world.ctx, slash("hunt"))       # ScriptRng(0.0) → shard
    assert item_qty(world, "ember_shard") == 1
    handlers.cmd_adventure(world.ctx, slash("adventure"))  # 0.0 → draught + wraithsilk
    assert item_qty(world, game.GREATER_LIFE_POTION) == 1
    assert item_qty(world, "wraithsilk") == 1


# --- Phase 3 game math ----------------------------------------------------------------------------

def test_resolve_duel_clamps_and_outcomes():
    david = {"level": 1, "atk": 0, "defense": 0}
    goliath = {"level": 50, "atk": 40, "defense": 17}
    assert game.resolve_duel(david, goliath, ScriptRng(0.10))["chance"] == 0.15  # floor
    assert game.resolve_duel(goliath, david, ScriptRng(0.10))["chance"] == 0.85  # ceiling
    assert game.resolve_duel(david, goliath, ScriptRng(0.0))["challenger_wins"] is True
    assert game.resolve_duel(david, goliath, ScriptRng(0.99))["challenger_wins"] is False


def test_guild_name_validation():
    assert game.validate_guild_name("Ash Wardens") is None
    assert game.validate_guild_name("Bo") is not None            # too short
    assert game.validate_guild_name("x" * 25) is not None        # too long
    assert game.validate_guild_name("Sm✨ke") is not None        # charset
    assert game.validate_guild_name("---") is not None           # needs alnum
    assert game.guild_key("The Ash Wardens") == "the_ash_wardens"


def test_arena_math():
    assert game.arena_day(0) == "1970-01-01"
    assert game.arena_day(86400) == "1970-01-02"
    assert sum(game.arena_payouts(1000)) <= 1000
    assert game.arena_pool(150, 2.0) == 150 + 200
    rng = random.Random(3)
    for _ in range(200):
        score = game.arena_score(5, 7, 2, rng)
        assert 15 + 14 + 2 <= score <= 15 + 14 + 2 + 50


# --- Phase 3: duels ----------------------------------------------------------------------------------

def duel_setup(world, bet=50, coins_c=100, coins_t=80, duel_id="duel1"):
    started(world)                       # challenger 200
    ready_hero(world, "300", level=1)    # target
    set_player(world, UID, coins=coins_c)
    set_player(world, "300", coins=coins_t)
    handlers.cmd_duel(world.ctx, slash(
        "duel", options=[{"name": "user", "value": "300"}, {"name": "bet", "value": bet}],
        interaction_id=duel_id))
    return duel_id


def test_duel_challenge_escrows_stake(world):
    duel_setup(world, bet=60)
    assert get_player(world)["coins"] == 40  # 100 − 60 escrowed
    response = last(world.ctx)
    assert response["content"] == "<@300>"
    ids = [c["custom_id"] for c in response["components"][0].to_dict()["components"]]
    assert f"{handlers.DUEL_ACCEPT_PREFIX}duel1" in ids
    assert f"{handlers.DUEL_DECLINE_PREFIX}duel1" in ids
    assert "no real-world value" in text_of(response)


def test_duel_rejects_self_unknown_and_broke(world):
    started(world)
    handlers.cmd_duel(world.ctx, slash("duel", options=[{"name": "user", "value": UID}]))
    assert "Shadow-boxing" in text_of(last(world.ctx))
    handlers.cmd_duel(world.ctx, slash("duel", options=[{"name": "user", "value": "404"}]))
    assert "hasn't entered" in text_of(last(world.ctx))
    ready_hero(world, "300")
    handlers.cmd_duel(world.ctx, slash(
        "duel", options=[{"name": "user", "value": "300"}, {"name": "bet", "value": 999}]))
    assert "can't cover" in text_of(last(world.ctx))
    assert world.conn.execute("SELECT COUNT(*) FROM duels").fetchone()[0] == 0


def test_duel_one_open_challenge_per_challenger(world):
    duel_setup(world, bet=10)            # coins: 100 → 90
    handlers.cmd_duel(world.ctx, slash(
        "duel", options=[{"name": "user", "value": "300"}, {"name": "bet", "value": 10}],
        interaction_id="duel2"))
    assert "already have an open challenge" in text_of(last(world.ctx))
    assert get_player(world)["coins"] == 90  # second escrow refunded
    assert world.conn.execute("SELECT COUNT(*) FROM duels").fetchone()[0] == 1


def test_duel_accept_only_by_target(world):
    duel_setup(world)
    ready_hero(world, "999")
    handlers.comp_duel_accept(world.ctx, press(f"{handlers.DUEL_ACCEPT_PREFIX}duel1", uid="999"))
    assert "isn't yours to answer" in text_of(last(world.ctx))
    handlers.comp_duel_accept(world.ctx, press(f"{handlers.DUEL_ACCEPT_PREFIX}duel1", uid=UID))
    assert "isn't yours to answer" in text_of(last(world.ctx))


def test_duel_accept_resolves_pot_and_cooldowns(world):
    duel_setup(world, bet=50)            # challenger 100→50 escrowed
    handlers.comp_duel_accept(world.ctx, press(f"{handlers.DUEL_ACCEPT_PREFIX}duel1", uid="300"))
    now = int(world.clock.now())
    challenger, target = get_player(world), get_player(world, "300")
    # ScriptRng(0.0): challenger wins the 100-Ember pot
    assert challenger["coins"] == 150 and target["coins"] == 30
    assert challenger["xp"] == game.DUEL_XP_WIN and target["xp"] == game.DUEL_XP_LOSS
    assert challenger["last_duel_at"] == now and target["last_duel_at"] == now
    assert challenger["hp"] == 100  # honor bouts leave no wounds
    row = world.conn.execute("SELECT status FROM duels WHERE id = 'duel1'").fetchone()
    assert row[0] == "resolved"
    assert "defeats" in text_of(last(world.ctx))
    assert any(m["metric"] == "duels_resolved" for m in world.ctx.metrics.recorded)
    # double-settlement guard
    handlers.comp_duel_accept(world.ctx, press(f"{handlers.DUEL_ACCEPT_PREFIX}duel1", uid="300"))
    assert "already settled or gone" in text_of(last(world.ctx))
    assert get_player(world)["coins"] == 150


def test_duel_target_can_win(world, monkeypatch):
    monkeypatch.setattr(handlers, "_rng", ScriptRng(0.99))
    duel_setup(world, bet=50)
    handlers.comp_duel_accept(world.ctx, press(f"{handlers.DUEL_ACCEPT_PREFIX}duel1", uid="300"))
    assert get_player(world, "300")["coins"] == 130  # 80 − 50 + 100 pot
    assert get_player(world)["coins"] == 50


def test_duel_decline_refunds_challenger(world):
    duel_setup(world, bet=40)
    handlers.comp_duel_decline(world.ctx, press(f"{handlers.DUEL_DECLINE_PREFIX}duel1", uid="300"))
    assert get_player(world)["coins"] == 100  # escrow returned
    row = world.conn.execute("SELECT status FROM duels WHERE id = 'duel1'").fetchone()
    assert row[0] == "declined"
    assert "declined" in text_of(last(world.ctx)).lower()


def test_duel_expires_lazily_with_refund(world):
    duel_setup(world, bet=30)
    world.clock.advance(game.DUEL_TTL + 1)
    handlers.comp_duel_accept(world.ctx, press(f"{handlers.DUEL_ACCEPT_PREFIX}duel1", uid="300"))
    assert "went cold" in text_of(last(world.ctx))
    assert get_player(world)["coins"] == 100
    row = world.conn.execute("SELECT status FROM duels WHERE id = 'duel1'").fetchone()
    assert row[0] == "expired"


def test_duel_broke_target_reopens_challenge(world):
    duel_setup(world, bet=50, coins_t=10)
    handlers.comp_duel_accept(world.ctx, press(f"{handlers.DUEL_ACCEPT_PREFIX}duel1", uid="300"))
    assert "can't cover" in text_of(last(world.ctx))
    row = world.conn.execute("SELECT status FROM duels WHERE id = 'duel1'").fetchone()
    assert row[0] == "open"                       # still answerable
    assert get_player(world)["coins"] == 50       # challenger escrow still held
    assert get_player(world, "300")["coins"] == 10


def test_duel_cooldown_after_resolution(world):
    duel_setup(world, bet=0)
    handlers.comp_duel_accept(world.ctx, press(f"{handlers.DUEL_ACCEPT_PREFIX}duel1", uid="300"))
    handlers.cmd_duel(world.ctx, slash(
        "duel", options=[{"name": "user", "value": "300"}], interaction_id="duel2"))
    assert "Honor needs rest" in text_of(last(world.ctx))
    world.clock.advance(game.DUEL_COOLDOWN + 1)
    handlers.cmd_duel(world.ctx, slash(
        "duel", options=[{"name": "user", "value": "300"}], interaction_id="duel2"))
    assert world.conn.execute(
        "SELECT COUNT(*) FROM duels WHERE status = 'open'").fetchone()[0] == 1


def test_duel_second_challenge_blocked_while_first_resolving(world):
    duel_setup(world, bet=10)            # coins 100 → 90
    world.conn.execute("UPDATE duels SET status = 'resolving' WHERE id = 'duel1'")
    world.conn.commit()
    handlers.cmd_duel(world.ctx, slash(
        "duel", options=[{"name": "user", "value": "300"}, {"name": "bet", "value": 10}],
        interaction_id="duel2"))
    assert "already have an open challenge" in text_of(last(world.ctx))
    assert get_player(world)["coins"] == 90  # second escrow refunded
    assert world.conn.execute("SELECT COUNT(*) FROM duels").fetchone()[0] == 1


def test_duel_accept_dissolves_if_challenger_dueled_elsewhere(world):
    duel_setup(world, bet=50)            # challenger escrowed: 100 → 50
    set_player(world, UID, last_duel_at=int(world.clock.now()))  # dueled elsewhere
    handlers.comp_duel_accept(world.ctx, press(f"{handlers.DUEL_ACCEPT_PREFIX}duel1", uid="300"))
    assert "dissolves" in text_of(last(world.ctx))
    assert get_player(world)["coins"] == 100                  # stake went home
    assert get_player(world, "300")["coins"] == 80            # target untouched
    assert get_player(world, "300")["last_duel_at"] == 0      # their claim refunded
    row = world.conn.execute("SELECT status FROM duels WHERE id = 'duel1'").fetchone()
    assert row[0] == "expired"


def test_duel_accept_blocked_when_target_on_cooldown(world):
    duel_setup(world, bet=50)
    set_player(world, "300", last_duel_at=int(world.clock.now()))
    handlers.comp_duel_accept(world.ctx, press(f"{handlers.DUEL_ACCEPT_PREFIX}duel1", uid="300"))
    assert "Honor needs rest" in text_of(last(world.ctx))
    row = world.conn.execute("SELECT status FROM duels WHERE id = 'duel1'").fetchone()
    assert row[0] == "open"                                   # challenge still stands
    assert get_player(world)["coins"] == 50                   # escrow still held


def test_duel_settlement_failure_voids_and_refunds_everything(world, monkeypatch):
    duel_setup(world, bet=50)

    def explode(*args, **kwargs):
        raise RuntimeError("RPC error (sql.execute): boom")

    monkeypatch.setattr(game, "resolve_duel", explode)
    handlers.comp_duel_accept(world.ctx, press(f"{handlers.DUEL_ACCEPT_PREFIX}duel1", uid="300"))
    assert "went wrong" in text_of(last(world.ctx))
    assert get_player(world)["coins"] == 100                  # challenger stake back
    assert get_player(world, "300")["coins"] == 80            # target stake back
    assert get_player(world, "300")["last_duel_at"] == 0      # cooldowns refunded
    assert get_player(world)["last_duel_at"] == 0
    row = world.conn.execute("SELECT status FROM duels WHERE id = 'duel1'").fetchone()
    assert row[0] == "expired"                                # never wedged in 'resolving'
    assert any(e["level"] == "error" for e in world.ctx.log_entries)


def test_duel_withdraw_by_challenger(world):
    duel_setup(world, bet=40)
    handlers.comp_duel_decline(world.ctx, press(f"{handlers.DUEL_DECLINE_PREFIX}duel1", uid=UID))
    assert "withdrawn" in text_of(last(world.ctx)).lower()
    assert "no real-world value" in text_of(last(world.ctx))
    assert get_player(world)["coins"] == 100
    row = world.conn.execute("SELECT status FROM duels WHERE id = 'duel1'").fetchone()
    assert row[0] == "declined"


# --- Phase 3: trades -----------------------------------------------------------------------------------

def trade_setup(world, qty=2, price=100, trade_id="trade1"):
    started(world)
    ready_hero(world, "300")
    give_items(world, UID, ember_tonic=3)
    set_player(world, "300", coins=150)
    handlers.cmd_trade(world.ctx, slash(
        "trade", options=[{"name": "user", "value": "300"},
                          {"name": "item", "value": "ember tonic"},
                          {"name": "price", "value": price},
                          {"name": "qty", "value": qty}],
        interaction_id=trade_id))
    return trade_id


def test_trade_offer_escrows_items(world):
    trade_setup(world)
    assert item_qty(world, game.LIFE_POTION) == 1  # 2 of 3 escrowed
    response = last(world.ctx)
    assert response["content"] == "<@300>"
    ids = [c["custom_id"] for c in response["components"][0].to_dict()["components"]]
    assert f"{handlers.TRADE_ACCEPT_PREFIX}trade1" in ids
    assert f"{handlers.TRADE_CANCEL_PREFIX}trade1" in ids


def test_trade_rejections(world):
    started(world)
    handlers.cmd_trade(world.ctx, slash("trade", options=[
        {"name": "user", "value": UID}, {"name": "item", "value": "ember tonic"},
        {"name": "price", "value": 10}]))
    assert "reflection" in text_of(last(world.ctx))
    handlers.cmd_trade(world.ctx, slash("trade", options=[
        {"name": "user", "value": "404"}, {"name": "item", "value": "ember tonic"},
        {"name": "price", "value": 10}]))
    assert "hasn't entered" in text_of(last(world.ctx))
    ready_hero(world, "300")
    handlers.cmd_trade(world.ctx, slash("trade", options=[
        {"name": "user", "value": "300"}, {"name": "item", "value": "fists"},
        {"name": "price", "value": 10}]))
    assert "nothing you can trade" in text_of(last(world.ctx))
    handlers.cmd_trade(world.ctx, slash("trade", options=[
        {"name": "user", "value": "300"}, {"name": "item", "value": "ember tonic"},
        {"name": "price", "value": 10}]))
    assert "don't carry" in text_of(last(world.ctx))


def test_trade_accept_transfers_goods_and_coins(world):
    trade_setup(world, qty=2, price=100)
    handlers.comp_trade_accept(world.ctx, press(f"{handlers.TRADE_ACCEPT_PREFIX}trade1", uid="300"))
    assert item_qty(world, game.LIFE_POTION, "300") == 2
    assert get_player(world, "300")["coins"] == 50
    assert get_player(world)["coins"] == 100
    row = world.conn.execute("SELECT status FROM trades WHERE id = 'trade1'").fetchone()
    assert row[0] == "resolved"
    assert "Deal struck" in text_of(last(world.ctx))


def test_trade_accept_guards(world):
    trade_setup(world, price=500)  # buyer only carries 150
    ready_hero(world, "999")
    handlers.comp_trade_accept(world.ctx, press(f"{handlers.TRADE_ACCEPT_PREFIX}trade1", uid="999"))
    assert "isn't addressed to you" in text_of(last(world.ctx))
    handlers.comp_trade_accept(world.ctx, press(f"{handlers.TRADE_ACCEPT_PREFIX}trade1", uid="300"))
    assert "can't cover" in text_of(last(world.ctx))
    row = world.conn.execute("SELECT status FROM trades WHERE id = 'trade1'").fetchone()
    assert row[0] == "open"                        # reopened for later
    assert item_qty(world, game.LIFE_POTION) == 1  # escrow still held


def test_trade_decline_cancel_and_expiry_refund(world):
    trade_setup(world)
    handlers.comp_trade_cancel(world.ctx, press(f"{handlers.TRADE_CANCEL_PREFIX}trade1", uid="300"))
    assert "Only the seller" in text_of(last(world.ctx))
    handlers.comp_trade_decline(world.ctx, press(f"{handlers.TRADE_DECLINE_PREFIX}trade1", uid="300"))
    assert item_qty(world, game.LIFE_POTION) == 3  # escrow returned

    handlers.cmd_trade(world.ctx, slash(
        "trade", options=[{"name": "user", "value": "300"},
                          {"name": "item", "value": "ember tonic"},
                          {"name": "price", "value": 10}], interaction_id="trade2"))
    assert item_qty(world, game.LIFE_POTION) == 2
    handlers.comp_trade_cancel(world.ctx, press(f"{handlers.TRADE_CANCEL_PREFIX}trade2", uid=UID))
    assert item_qty(world, game.LIFE_POTION) == 3

    handlers.cmd_trade(world.ctx, slash(
        "trade", options=[{"name": "user", "value": "300"},
                          {"name": "item", "value": "ember tonic"},
                          {"name": "price", "value": 10}], interaction_id="trade3"))
    world.clock.advance(game.TRADE_TTL + 1)
    handlers.comp_trade_accept(world.ctx, press(f"{handlers.TRADE_ACCEPT_PREFIX}trade3", uid="300"))
    assert "lapsed" in text_of(last(world.ctx))
    assert item_qty(world, game.LIFE_POTION) == 3  # expiry refund


def test_trade_gift_and_one_open_per_seller(world):
    trade_setup(world, qty=1, price=0)
    handlers.cmd_trade(world.ctx, slash(
        "trade", options=[{"name": "user", "value": "300"},
                          {"name": "item", "value": "ember tonic"},
                          {"name": "price", "value": 10}], interaction_id="trade2"))
    assert "already have an open offer" in text_of(last(world.ctx))
    assert item_qty(world, game.LIFE_POTION) == 2  # second escrow refunded
    handlers.comp_trade_accept(world.ctx, press(f"{handlers.TRADE_ACCEPT_PREFIX}trade1", uid="300"))
    assert item_qty(world, game.LIFE_POTION, "300") == 1
    assert get_player(world, "300")["coins"] == 150  # gift: no payment


def test_trade_away_equipped_gear_unstraps_it(world):
    started(world)
    ready_hero(world, "300")
    set_player(world, UID, coins=200)
    handlers.cmd_buy(world.ctx, slash("buy", options=[{"name": "item", "value": "charstick"}]))
    assert get_player(world)["sword"] == "charstick"
    handlers.cmd_trade(world.ctx, slash(
        "trade", options=[{"name": "user", "value": "300"},
                          {"name": "item", "value": "charstick"},
                          {"name": "price", "value": 10}], interaction_id="tr-gear"))
    player = get_player(world)
    assert player["sword"] == game.DEFAULT_SWORD  # no phantom stats while escrowed
    assert "unstrap" in text_of(last(world.ctx))


def test_trade_settlement_failure_burns_not_mints(world, monkeypatch):
    # Burn-bias contract: the terminal flip lands BEFORE the grant, so an
    # ambiguous grant failure can never refund escrow for goods that may have
    # been delivered. The failure burns — no value is duplicated anywhere.
    trade_setup(world, qty=2, price=100)
    real_add_item = handlers._add_item

    def explode_for_buyer(ctx, user_id, item_id, amount=1, epoch=None):
        if user_id == "300":
            raise RuntimeError("RPC error (sql.execute): boom")
        return real_add_item(ctx, user_id, item_id, amount, epoch=epoch)

    monkeypatch.setattr(handlers, "_add_item", explode_for_buyer)
    handlers.comp_trade_accept(world.ctx, press(f"{handlers.TRADE_ACCEPT_PREFIX}trade1", uid="300"))
    assert "went wrong" in text_of(last(world.ctx))
    row = world.conn.execute("SELECT status FROM trades WHERE id = 'trade1'").fetchone()
    assert row[0] == "resolved"                               # closed, never wedged
    assert get_player(world, "300")["coins"] == 50            # buyer paid (burn-bias)
    assert item_qty(world, game.LIFE_POTION, "300") == 0      # goods burned, not duped
    assert item_qty(world, game.LIFE_POTION) == 1             # escrow NOT re-minted
    assert get_player(world)["coins"] == 100                  # seller credit still landed
    # a later sweep must not resurrect anything either
    world.clock.advance(game.TRADE_TTL + 700)
    handlers._sweep_stale_trades(world.ctx, int(world.clock.now()))
    assert item_qty(world, game.LIFE_POTION) == 1


def test_social_tables_are_pruned(world):
    started(world)
    old = int(world.clock.now()) - 8 * 86400
    world.conn.execute(
        "INSERT INTO duels (id, challenger_id, target_id, bet, status, created_at) "
        "VALUES ('old-d', '1', '2', 0, 'resolved', ?)", [old])
    world.conn.execute(
        "INSERT INTO trades (id, seller_id, buyer_id, item_id, qty, price, status, created_at) "
        "VALUES ('old-t', '1', '2', 'ember_tonic', 1, 0, 'declined', ?)", [old])
    world.conn.commit()
    handlers._sweep_stale_duels(world.ctx, int(world.clock.now()))
    handlers._sweep_stale_trades(world.ctx, int(world.clock.now()))
    assert world.conn.execute("SELECT COUNT(*) FROM duels").fetchone()[0] == 0
    assert world.conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0] == 0


# --- Phase 3: guilds -------------------------------------------------------------------------------------

def guild_setup(world):
    started(world)
    set_player(world, UID, coins=3000)
    handlers.cmd_guild(world.ctx, slash("guild", options=[
        {"name": "action", "value": "create"}, {"name": "name", "value": "Ash Wardens"}]))


def test_guild_create_founds_and_charges(world):
    guild_setup(world)
    assert get_player(world)["coins"] == 500
    guild = world.conn.execute("SELECT * FROM guilds").fetchone()
    assert guild is not None
    assert "founded" in text_of(last(world.ctx))
    member = world.conn.execute("SELECT guild_key FROM guild_members WHERE user_id = ?",
                                [UID]).fetchone()
    assert member[0] == "ash_wardens"


def test_guild_create_guards(world):
    started(world)
    handlers.cmd_guild(world.ctx, slash("guild", options=[
        {"name": "action", "value": "create"}, {"name": "name", "value": "Yo"}]))
    assert "3–24 characters" in text_of(last(world.ctx))
    handlers.cmd_guild(world.ctx, slash("guild", options=[
        {"name": "action", "value": "create"}, {"name": "name", "value": "Ash Wardens"}]))
    assert "costs" in text_of(last(world.ctx))
    guild_setup(world)
    ready_hero(world, "300")
    set_player(world, "300", coins=3000)
    handlers.cmd_guild(world.ctx, slash("guild", uid="300", options=[
        {"name": "action", "value": "create"}, {"name": "name", "value": "ash wardens"}]))
    assert "already bears" in text_of(last(world.ctx))
    assert get_player(world, "300")["coins"] == 3000  # fee refunded
    handlers.cmd_guild(world.ctx, slash("guild", options=[
        {"name": "action", "value": "create"}, {"name": "name", "value": "Second Banner"}]))
    assert "already sworn" in text_of(last(world.ctx))


def test_guild_join_cap_and_double_membership(world):
    guild_setup(world)
    ready_hero(world, "300")
    handlers.cmd_guild(world.ctx, slash("guild", uid="300", options=[
        {"name": "action", "value": "join"}, {"name": "name", "value": "Ash Wardens"}]))
    assert "joins" in text_of(last(world.ctx))
    assert world.conn.execute("SELECT member_count FROM guilds").fetchone()[0] == 2
    handlers.cmd_guild(world.ctx, slash("guild", uid="300", options=[
        {"name": "action", "value": "join"}, {"name": "name", "value": "Ash Wardens"}]))
    assert "already sworn" in text_of(last(world.ctx))
    world.conn.execute("UPDATE guilds SET member_count = ?", [game.GUILD_MAX_MEMBERS])
    world.conn.commit()
    ready_hero(world, "400")
    handlers.cmd_guild(world.ctx, slash("guild", uid="400", options=[
        {"name": "action", "value": "join"}, {"name": "name", "value": "Ash Wardens"}]))
    assert "full" in text_of(last(world.ctx))


def test_guild_leave_succession_and_disband(world):
    guild_setup(world)
    ready_hero(world, "300")
    handlers.cmd_guild(world.ctx, slash("guild", uid="300", options=[
        {"name": "action", "value": "join"}, {"name": "name", "value": "Ash Wardens"}]))
    handlers.cmd_guild(world.ctx, slash("guild", options=[{"name": "action", "value": "leave"}]))
    assert "carries the banner" in text_of(last(world.ctx))
    assert world.conn.execute("SELECT leader_id FROM guilds").fetchone()[0] == "300"
    handlers.cmd_guild(world.ctx, slash("guild", uid="300",
                                        options=[{"name": "action", "value": "leave"}]))
    assert "folded away" in text_of(last(world.ctx))
    assert world.conn.execute("SELECT COUNT(*) FROM guilds").fetchone()[0] == 0


def test_guild_info_and_profile_tag(world):
    guild_setup(world)
    handlers.cmd_guild(world.ctx, slash("guild", options=[{"name": "action", "value": "info"}]))
    body = text_of(last(world.ctx))
    assert "Ash Wardens" in body and "👑" in body
    handlers.cmd_profile(world.ctx, slash("profile"))
    assert "Ash Wardens" in text_of(last(world.ctx))


def test_guild_leaderboard_metric(world):
    guild_setup(world)
    set_player(world, UID, level=10)
    ready_hero(world, "300", level=7)
    set_player(world, "300", coins=3000)
    handlers.cmd_guild(world.ctx, slash("guild", uid="300", options=[
        {"name": "action", "value": "create"}, {"name": "name", "value": "Cinder Pact"}]))
    handlers.cmd_top(world.ctx, slash("top",
                                              options=[{"name": "metric", "value": "guild"}]))
    body = text_of(last(world.ctx))
    assert body.index("**Ash Wardens**") < body.index("**Cinder Pact**")
    assert "10 combined levels" in body and "7 combined levels" in body


def test_guild_leadership_self_heals(world):
    guild_setup(world)
    ready_hero(world, "300")
    handlers.cmd_guild(world.ctx, slash("guild", uid="300", options=[
        {"name": "action", "value": "join"}, {"name": "name", "value": "Ash Wardens"}]))
    world.conn.execute("UPDATE guilds SET leader_id = '999'")  # leader vanished mid-race
    world.conn.commit()
    handlers.cmd_guild(world.ctx, slash("guild", options=[{"name": "action", "value": "info"}]))
    assert world.conn.execute("SELECT leader_id FROM guilds").fetchone()[0] == UID
    assert "👑" in text_of(last(world.ctx))


# --- Phase 3: arena ----------------------------------------------------------------------------------------

def test_arena_enter_charges_fee_and_scores(world):
    started(world)
    set_player(world, coins=100)
    handlers.cmd_arena(world.ctx, slash("arena"))
    player = get_player(world)
    assert player["coins"] == 50 and player["xp"] == game.ARENA_XP
    today = game.arena_day(int(world.clock.now()))
    entry = world.conn.execute(
        "SELECT score FROM arena_entries WHERE day = ? AND user_id = ?",
        [today, UID]).fetchone()
    assert entry[0] == 3  # level 1×3 + 0 atk + 0 def + roll 0 (ScriptRng)
    body = text_of(last(world.ctx))
    assert "your score today" in body and "prize pool" in body
    ids = [c["custom_id"] for c in last(world.ctx)["components"][0].to_dict()["components"]]
    assert f"{handlers.ARENA_JOIN_PREFIX}{today}" in ids


def test_arena_single_entry_per_day(world):
    started(world)
    set_player(world, coins=100)
    handlers.cmd_arena(world.ctx, slash("arena"))
    handlers.cmd_arena(world.ctx, slash("arena"))
    assert get_player(world)["coins"] == 50  # no second fee
    today = game.arena_day(int(world.clock.now()))
    handlers.comp_arena_join(world.ctx, press(f"{handlers.ARENA_JOIN_PREFIX}{today}"))
    assert "already on the sand" in text_of(last(world.ctx))
    assert get_player(world)["coins"] == 50


def test_arena_join_button_open_to_all_with_day_gate(world):
    started(world)
    set_player(world, coins=100)
    handlers.cmd_arena(world.ctx, slash("arena"))
    today = game.arena_day(int(world.clock.now()))
    ready_hero(world, "300")
    set_player(world, "300", coins=60)
    handlers.comp_arena_join(world.ctx, press(f"{handlers.ARENA_JOIN_PREFIX}{today}", uid="300"))
    assert get_player(world, "300")["coins"] == 10
    handlers.comp_arena_join(world.ctx, press(f"{handlers.ARENA_JOIN_PREFIX}1969-12-31", uid="300"))
    assert "gates have closed" in text_of(last(world.ctx))


def test_arena_resolves_yesterday_exactly_once(world):
    for uid, name, score in (("A1", "Aria", 90), ("B2", "Borin", 70),
                             ("C3", "Cinder", 50), ("D4", "Dag", 10)):
        ready_hero(world, uid, name=name)
    yesterday = game.arena_day(int(world.clock.now()))
    for uid, name, score in (("A1", "Aria", 90), ("B2", "Borin", 70),
                             ("C3", "Cinder", 50), ("D4", "Dag", 10)):
        world.conn.execute(
            "INSERT INTO arena_entries (day, user_id, username, score, paid_fee, "
            "channel_id, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            [yesterday, uid, name, score, game.ARENA_FEE, "77", int(world.clock.now())])
    world.conn.commit()

    world.clock.advance(86400)  # a new UTC day dawns
    started(world)
    set_player(world, coins=100)
    handlers.cmd_arena(world.ctx, slash("arena"))  # first arena touch resolves yesterday

    # pool = 4×50 fees + 100 bonus = 300 → podium 150/90/60
    assert get_player(world, "A1")["coins"] == 150
    assert get_player(world, "B2")["coins"] == 90
    assert get_player(world, "C3")["coins"] == 60
    assert get_player(world, "D4")["coins"] == 0
    broadcasts = [m for m in world.ctx.discord.messages_sent if m.get("embeds")]
    assert broadcasts and "Arena results" in broadcasts[-1]["embeds"][0]["title"]
    assert broadcasts[-1]["channel_id"] == "77"

    handlers.cmd_arena(world.ctx, slash("arena"))  # no double payout
    assert get_player(world, "A1")["coins"] == 150
    assert world.conn.execute("SELECT COUNT(*) FROM arena_days").fetchone()[0] == 1


def test_arena_entry_refunded_when_day_already_settled(world):
    started(world)
    set_player(world, coins=100)
    today = game.arena_day(int(world.clock.now()))
    world.conn.execute("INSERT INTO arena_days (day, resolved_at) VALUES (?, ?)",
                       [today, int(world.clock.now())])
    world.conn.commit()
    handlers.cmd_arena(world.ctx, slash("arena"))
    assert "day turned" in text_of(last(world.ctx))
    assert get_player(world)["coins"] == 100  # fee refunded, not swallowed
    assert world.conn.execute("SELECT COUNT(*) FROM arena_entries").fetchone()[0] == 0


def test_arena_fee_disclosed_on_standings(world):
    started(world)
    set_player(world, coins=100)
    handlers.cmd_arena(world.ctx, slash("arena"))
    assert f"Entry fee: **{game.ARENA_FEE}" in text_of(last(world.ctx))


def test_arena_pool_scales_with_multiplier(world):
    world.ctx.kv.set(handlers.KV_MULTIPLIER, 2.0)
    ready_hero(world, "A1", name="Aria")
    yesterday = game.arena_day(int(world.clock.now()))
    world.conn.execute(
        "INSERT INTO arena_entries (day, user_id, username, score, paid_fee, "
        "channel_id, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        [yesterday, "A1", "Aria", 42, game.ARENA_FEE, "1", int(world.clock.now())])
    world.conn.commit()
    world.clock.advance(86400)
    started(world)
    set_player(world, coins=100)
    handlers.cmd_arena(world.ctx, slash("arena"))
    # pool = 50 fees + 100×2.0 bonus = 250 → winner takes 50% = 125
    assert get_player(world, "A1")["coins"] == 125


# --- Phase 4 game math ----------------------------------------------------------------------------

class SeqRng:
    """random() pops scripted values (then 0.0); randint() returns the lower
    bound; choice() the first element — for steering lootbox slots."""

    def __init__(self, randoms=()):
        self.randoms = list(randoms)

    def random(self):
        return self.randoms.pop(0) if self.randoms else 0.0

    def randint(self, a, b):
        return a

    def choice(self, seq):
        return seq[0]


def test_player_boosts_compose_and_cap():
    plain = game.player_boosts("", 0, None)
    assert plain["coins"] == 1.0 and plain["xp"] == 1.0 and plain["drop"] == 0.0
    assert game.player_boosts("cinderpup", 0, None)["coins"] == pytest.approx(1.10)
    assert game.player_boosts("", 2, None)["coins"] == pytest.approx(1.20)
    assert game.player_boosts("", 99, None)["coins"] == pytest.approx(1.50)  # capped at 5
    season = game.SEASONS[0]
    combo = game.player_boosts("cinderpup", 1, season)
    assert combo["coins"] == pytest.approx(1.10 * 1.10 * 1.25)
    assert combo["season_mob"] == game.SEASON_MOBS[season["key"]]
    assert game.player_boosts("pyrelet", 0, None)["arena"] == 10
    assert game.player_boosts("emberowl", 0, None)["quest"] == pytest.approx(0.25)


def test_current_season_windows_and_wrap():
    def ts(day_of_1970):
        return day_of_1970 * 86400
    assert game.current_season(ts(11)) is None                      # Jan 12 — quiet
    assert game.current_season(ts(95))["key"] == "ashbloom"         # Apr 6
    assert game.current_season(ts(161))["key"] == "highsun"         # Jun 11
    assert game.current_season(ts(358))["key"] == "frostflame"      # Dec 25
    assert game.current_season(ts(366))["key"] == "frostflame"      # Jan 2 — wraps
    assert game.current_season(ts(369)) is None                     # Jan 5 — over


def test_roll_lootbox_slots_and_duplicates():
    coins = game.roll_lootbox("ember_cache", SeqRng([0.0]), [])
    assert coins == {"kind": "coins", "amount": 150, "jackpot": False, "duplicate_pet": None}
    jackpot = game.roll_lootbox("ember_cache", SeqRng([0.995]), [])
    assert jackpot["jackpot"] is True and jackpot["amount"] == 2000
    pet = game.roll_lootbox("ember_cache", SeqRng([0.93]), [])
    assert pet == {"kind": "pet", "item_id": "ashwhisker"}  # first of sorted PET_PERKS
    dup = game.roll_lootbox("ember_cache", SeqRng([0.93]), ["ashwhisker"])
    assert dup["kind"] == "coins" and dup["amount"] == game.DUPLICATE_PET_COINS
    assert dup["duplicate_pet"] == "ashwhisker"
    for box in game.LOOT_TABLES:
        assert sum(w for w, _ in game.lootbox_odds(box)) == 100


def test_daily_quests_deterministic_and_bounded():
    first = game.daily_quests("200", "2026-06-12")
    again = game.daily_quests("200", "2026-06-12")
    assert first == again and len(first) == game.QUESTS_PER_DAY
    assert len({q["key"] for q in first}) == game.QUESTS_PER_DAY  # distinct types
    for quest in first:
        lo, hi = game.QUEST_TYPES[quest["key"]]["target"]
        assert lo <= quest["target"] <= hi
        assert str(quest["target"]) in quest["desc"] or quest["target"] == 1
    assert game.daily_quests("201", "2026-06-12") != first or \
           game.daily_quests("200", "2026-06-13") != first  # seeds actually vary


def test_quest_reward_scaling():
    quest = {"reward": 100}
    assert game.quest_reward(quest, 1) == 110
    assert game.quest_reward(quest, 10) == 200
    assert game.quest_reward(quest, 10, {"quest": 0.25}) == 250


# --- Phase 4: /equip --------------------------------------------------------------------------------

def test_equip_swaps_owned_gear_freely(world):
    started(world)
    set_player(world, coins=1000)
    handlers.cmd_buy(world.ctx, slash("buy", options=[{"name": "item", "value": "charstick"}]))
    handlers.cmd_buy(world.ctx, slash("buy", options=[{"name": "item", "value": "cinder saber"}]))
    set_player(world, sword_enchant=2)
    assert get_player(world)["sword"] == "cinder_saber"
    # a deliberate downgrade is allowed — and clears the enchant
    handlers.cmd_equip(world.ctx, slash("equip", options=[{"name": "item", "value": "charstick"}]))
    player = get_player(world)
    assert player["sword"] == "charstick" and player["sword_enchant"] == 0
    assert "enchantment fades" in text_of(last(world.ctx))
    # bare fists are always available
    handlers.cmd_equip(world.ctx, slash("equip", options=[{"name": "item", "value": "fists"}]))
    assert get_player(world)["sword"] == "fists"
    # unowned gear is not
    handlers.cmd_equip(world.ctx, slash("equip", options=[{"name": "item", "value": "dawnforged blade"}]))
    assert "don't own" in text_of(last(world.ctx))
    handlers.cmd_equip(world.ctx, slash("equip", options=[{"name": "item", "value": "ember tonic"}]))
    assert "swords and armor" in text_of(last(world.ctx))


# --- Phase 4: pets -----------------------------------------------------------------------------------

def test_pet_set_and_perk_applies(world):
    started(world)
    give_items(world, UID, cinderpup=1)
    handlers.cmd_pet(world.ctx, slash("pet", options=[{"name": "name", "value": "Cinderpup"}]))
    assert get_player(world)["pet"] == "cinderpup"
    assert "takes the lead" in text_of(last(world.ctx))
    handlers.cmd_hunt(world.ctx, slash("hunt"))
    assert get_player(world)["coins"] == 11  # 10 × 1.10 companion boost


def test_pet_board_and_owner_scoped_buttons(world):
    started(world)
    handlers.cmd_pet(world.ctx, slash("pet"))
    assert "hatch from Ember Caches" in text_of(last(world.ctx))
    give_items(world, UID, cinderpup=1, wisplight=1)
    handlers.cmd_pet(world.ctx, slash("pet"))
    buttons = last(world.ctx)["components"][0].to_dict()["components"]
    assert any(b["custom_id"] == f"{handlers.PET_SET_PREFIX}cinderpup:{UID}" for b in buttons)
    handlers.comp_pet_set(world.ctx, press(f"{handlers.PET_SET_PREFIX}cinderpup:{UID}", uid="999"))
    assert "isn't yours" in text_of(last(world.ctx))
    handlers.comp_pet_set(world.ctx, press(f"{handlers.PET_SET_PREFIX}cinderpup:{UID}"))
    assert get_player(world)["pet"] == "cinderpup"


def test_pet_unowned_cannot_be_set(world):
    started(world)
    handlers.cmd_pet(world.ctx, slash("pet", options=[{"name": "name", "value": "Pyrelet"}]))
    assert "answers your whistle" in text_of(last(world.ctx))
    assert get_player(world)["pet"] == ""


def test_selling_active_pet_clears_leash(world):
    started(world)
    give_items(world, UID, cinderpup=1)
    handlers.cmd_pet(world.ctx, slash("pet", options=[{"name": "name", "value": "cinderpup"}]))
    handlers.cmd_sell(world.ctx, slash("sell", options=[{"name": "item", "value": "cinderpup"}]))
    player = get_player(world)
    assert player["pet"] == "" and player["coins"] == 1000
    assert "leash hangs empty" in text_of(last(world.ctx))


def test_trading_active_pet_suspends_and_restores_on_decline(world):
    started(world)
    ready_hero(world, "300")
    give_items(world, UID, cinderpup=1)
    handlers.cmd_pet(world.ctx, slash("pet", options=[{"name": "name", "value": "cinderpup"}]))
    handlers.cmd_trade(world.ctx, slash(
        "trade", options=[{"name": "user", "value": "300"},
                          {"name": "item", "value": "cinderpup"},
                          {"name": "price", "value": 500}], interaction_id="tr-pet"))
    assert get_player(world)["pet"] == ""  # perk suspended while in escrow
    handlers.comp_trade_decline(world.ctx, press(f"{handlers.TRADE_DECLINE_PREFIX}tr-pet", uid="300"))
    assert get_player(world)["pet"] == "cinderpup"  # came home, re-leashed
    assert item_qty(world, "cinderpup") == 1


# --- Phase 4: lootboxes ---------------------------------------------------------------------------------

def test_open_cache_grants_and_chains(world):
    started(world)
    give_items(world, UID, ember_cache=2)
    handlers.cmd_open(world.ctx, slash("open"))
    assert get_player(world)["coins"] == 150  # ScriptRng: first slot, low roll
    assert item_qty(world, "ember_cache") == 1
    response = last(world.ctx)
    assert response["ephemeral"] is True
    button = response["components"][0].to_dict()["components"][0]
    assert button["custom_id"] == f"{handlers.LOOT_AGAIN_PREFIX}ember_cache:{UID}"
    assert not button.get("disabled")
    handlers.comp_loot_again(world.ctx, press(f"{handlers.LOOT_AGAIN_PREFIX}ember_cache:{UID}"))
    assert item_qty(world, "ember_cache") == 0
    assert get_player(world)["coins"] == 300
    button = last(world.ctx)["components"][0].to_dict()["components"][0]
    assert button.get("disabled")
    assert any(m["metric"] == "caches_opened" for m in world.ctx.metrics.recorded)


def test_open_without_cache_shows_odds(world):
    started(world)
    handlers.cmd_open(world.ctx, slash("open"))
    body = text_of(last(world.ctx))
    assert "what's inside?" in body and "JACKPOT" in body and "%" in body
    assert "no real-world value" in body


def test_open_hatches_pet_and_converts_duplicates(world, monkeypatch):
    started(world)
    give_items(world, UID, ember_cache=2)
    monkeypatch.setattr(handlers, "_rng", SeqRng([0.93, 0.93]))
    handlers.cmd_open(world.ctx, slash("open"))
    player = get_player(world)
    assert item_qty(world, "ashwhisker") == 1
    assert player["pet"] == "ashwhisker"          # auto-adopted onto the empty leash
    assert last(world.ctx)["ephemeral"] is False  # a new companion is worth showing off
    handlers.comp_loot_again(world.ctx, press(f"{handlers.LOOT_AGAIN_PREFIX}ember_cache:{UID}"))
    assert get_player(world)["coins"] == game.DUPLICATE_PET_COINS
    assert "already" in text_of(last(world.ctx))


def test_open_button_owner_scoped_and_buyable_in_shop(world):
    started(world)
    handlers.comp_loot_again(world.ctx, press(f"{handlers.LOOT_AGAIN_PREFIX}ember_cache:{UID}", uid="999"))
    assert "isn't yours" in text_of(last(world.ctx))
    set_player(world, coins=500)
    handlers.cmd_buy(world.ctx, slash("buy", options=[{"name": "item", "value": "ember cache"}]))
    assert item_qty(world, "ember_cache") == 1
    handlers.comp_shop_page(world.ctx, press(f"{handlers.SHOP_PAGE_PREFIX}3:{UID}"))
    assert "Ember Cache" in text_of(last(world.ctx))


# --- Phase 4: quests ------------------------------------------------------------------------------------

def quest_keys_for(world, uid=UID):
    day = game.arena_day(int(world.clock.now()))
    return [q["key"] for q in game.daily_quests(uid, day)]


def test_quest_board_shows_three_and_progress_hooks_fire(world):
    started(world)
    handlers.cmd_questlog(world.ctx, slash("questlog"))
    body = text_of(last(world.ctx))
    assert body.count("▫️") + body.count("🎁") + body.count("✅") == game.QUESTS_PER_DAY
    assert "redraws when the day turns" in body
    # find a uid whose board includes hunt_win, then verify the hook ticks it
    uid = next(str(n) for n in range(1000, 1100)
               if "hunt_win" in [q["key"] for q in game.daily_quests(
                   str(n), game.arena_day(int(world.clock.now())))])
    ready_hero(world, uid, level=1)
    handlers.cmd_hunt(world.ctx, slash("hunt", uid=uid))
    day = game.arena_day(int(world.clock.now()))
    idx = [q["key"] for q in game.daily_quests(uid, day)].index("hunt_win")
    row = world.conn.execute(
        "SELECT progress FROM quest_progress WHERE user_id = ? AND day = ? AND quest_idx = ?",
        [uid, day, idx]).fetchone()
    assert row[0] == 1


def test_quest_claim_pays_once_and_is_owner_scoped(world):
    started(world)
    day = game.arena_day(int(world.clock.now()))
    quests = game.daily_quests(UID, day)
    target_idx = 0
    quest = quests[target_idx]
    for _ in range(quest["target"]):
        handlers._quest_event(world.ctx, UID, quest["key"], int(world.clock.now()))
    handlers.cmd_questlog(world.ctx, slash("questlog"))
    buttons = last(world.ctx)["components"][0].to_dict()["components"]
    claim_id = f"{handlers.QUEST_CLAIM_PREFIX}{target_idx}:{UID}"
    assert any(b["custom_id"] == claim_id for b in buttons)

    handlers.comp_quest_claim(world.ctx, press(claim_id, uid="999"))
    assert "isn't yours" in text_of(last(world.ctx))
    handlers.comp_quest_claim(world.ctx, press(claim_id))
    expected = game.quest_reward(quest, 1)
    player = get_player(world)
    assert player["coins"] == expected and player["xp"] == game.QUEST_XP
    handlers.comp_quest_claim(world.ctx, press(claim_id))  # double-claim
    assert "already claimed" in text_of(last(world.ctx))
    assert get_player(world)["coins"] == expected


def test_quest_claim_rejects_incomplete_and_prunes_old(world):
    started(world)
    handlers.comp_quest_claim(world.ctx, press(f"{handlers.QUEST_CLAIM_PREFIX}0:{UID}"))
    assert "Not finished yet" in text_of(last(world.ctx))
    old_day = game.arena_day(int(world.clock.now()) - 10 * 86400)
    world.conn.execute(
        "INSERT INTO quest_progress (user_id, day, quest_idx, progress, claimed) "
        "VALUES (?, ?, 0, 5, 1)", [UID, old_day])
    world.conn.commit()
    handlers.cmd_questlog(world.ctx, slash("questlog"))
    assert world.conn.execute("SELECT COUNT(*) FROM quest_progress WHERE day = ?",
                              [old_day]).fetchone()[0] == 0


# --- Phase 4: rekindling ----------------------------------------------------------------------------------

def test_rekindle_gate_below_twenty(world):
    started(world)
    handlers.cmd_rekindle(world.ctx, slash("rekindle"))
    assert "level **20+**" in text_of(last(world.ctx))
    assert get_player(world)["rekindles"] == 0


def test_rekindle_confirm_resets_and_rewards(world):
    started(world)
    set_player(world, level=22, xp=game.xp_for_level(22) + 5, coins=9999,
               sword="ironspark_blade", sword_enchant=3, max_hp=310, hp=200,
               last_daily_at=777)
    give_items(world, UID, ember_shard=10, cinderpup=1, ember_cache=2)
    handlers.cmd_pet(world.ctx, slash("pet", options=[{"name": "name", "value": "cinderpup"}]))

    handlers.cmd_rekindle(world.ctx, slash("rekindle"))
    warning = last(world.ctx)
    assert "cannot be undone" in text_of(warning)
    assert "stays at your side" in text_of(warning)
    confirm_id = warning["components"][0].to_dict()["components"][0]["custom_id"]
    assert confirm_id == f"{handlers.REKINDLE_CONFIRM_PREFIX}{UID}"

    handlers.comp_rekindle_confirm(world.ctx, press(confirm_id, uid="999"))
    assert "isn't yours" in text_of(last(world.ctx))
    handlers.comp_rekindle_confirm(world.ctx, press(confirm_id))
    player = get_player(world)
    assert player["level"] == 1 and player["xp"] == 0 and player["coins"] == 0
    assert player["sword"] == "fists" and player["sword_enchant"] == 0
    assert player["max_hp"] == 100 and player["hp"] == 100
    assert player["rekindles"] == 1
    assert player["last_daily_at"] == 777          # no bonus daily from resetting
    assert player["pet"] == "cinderpup"            # companion stays
    assert item_qty(world, "cinderpup") == 1
    assert item_qty(world, "ember_shard") == 0     # everything else burns
    assert item_qty(world, "ember_cache") == 0
    assert "rekindles their flame! (×1)" in text_of(last(world.ctx))

    # a stale second press finds a level-1 hero and refuses
    handlers.comp_rekindle_confirm(world.ctx, press(confirm_id))
    assert "isn't ready" in text_of(last(world.ctx))
    assert get_player(world)["rekindles"] == 1

    # the permanent bonus actually pays: daily is boosted by 10%
    handlers.cmd_daily(world.ctx, slash("daily"))
    expected = max(1, round(game.daily_reward(1, 1.0)["coins"]
                            * game.player_boosts("cinderpup", 1, None)["coins"]))
    assert get_player(world)["coins"] == expected
    handlers.cmd_profile(world.ctx, slash("profile"))
    assert "🔥×1" in text_of(last(world.ctx))


def test_rekindle_burns_open_escrows_too(world):
    # An open challenge's stake must not resurrect post-burn via a late decline.
    started(world)
    ready_hero(world, "300")
    set_player(world, UID, level=22, coins=500)
    handlers.cmd_duel(world.ctx, slash(
        "duel", options=[{"name": "user", "value": "300"}, {"name": "bet", "value": 200}],
        interaction_id="rk-duel"))
    give_items(world, UID, ember_tonic=2)
    assert get_player(world)["coins"] == 300  # 200 escrowed

    handlers.cmd_rekindle(world.ctx, slash("rekindle"))
    handlers.comp_rekindle_confirm(world.ctx, press(f"{handlers.REKINDLE_CONFIRM_PREFIX}{UID}"))
    player = get_player(world)
    assert player["rekindles"] == 1 and player["coins"] == 0
    duel_status = world.conn.execute("SELECT status FROM duels WHERE id = 'rk-duel'").fetchone()
    assert duel_status[0] == "expired"  # withdrawn before the burn

    # a late decline press cannot refund anything anymore
    handlers.comp_duel_decline(world.ctx, press(f"{handlers.DUEL_DECLINE_PREFIX}rk-duel", uid="300"))
    assert get_player(world)["coins"] == 0


# --- Phase 4: seasons ----------------------------------------------------------------------------------------

def test_seasonal_boost_and_creature_in_hunts(world):
    # march the world clock to April 6 — deep in the Ashbloom
    now = int(world.clock.now())
    world.clock.advance(95 * 86400 - now)
    started(world)
    handlers.cmd_hunt(world.ctx, slash("hunt"))
    body = text_of(last(world.ctx))
    # ScriptRng(0.0): the season roll converts the encounter to the bonus creature
    assert "Bloomwisp" in body and "creature of the season" in body
    expected = max(1, round(10 * 1.25 * game.SEASON_MOB_BONUS))
    assert get_player(world)["coins"] == expected
    handlers.cmd_questlog(world.ctx, slash("questlog"))
    assert "Ashbloom" in text_of(last(world.ctx))
    handlers.cmd_profile(world.ctx, slash("profile"))
    assert "burns" in text_of(last(world.ctx))


def test_epoch_guard_buries_stale_settlements(world):
    # THE prestige-farming blocker: a settle computed from a pre-Rekindle read
    # must not resurrect the old character via the level/max_hp ratchet.
    started(world)
    set_player(world, level=22, xp=game.xp_for_level(22), coins=500)
    stale = get_player(world)  # what a concurrent handler would have read
    handlers.cmd_rekindle(world.ctx, slash("rekindle"))
    handlers.comp_rekindle_confirm(world.ctx, press(f"{handlers.REKINDLE_CONFIRM_PREFIX}{UID}"))
    assert get_player(world)["level"] == 1 and get_player(world)["rekindles"] == 1

    handlers._settle_social(world.ctx, stale, 500, 1000, int(world.clock.now()))
    player = get_player(world)
    assert player["level"] == 1 and player["xp"] == 0 and player["coins"] == 0
    assert player["max_hp"] == 100  # the ratchet did NOT resurrect level 22

    # stale grants and credits burn the same way
    assert handlers._add_item(world.ctx, UID, "ember_shard", epoch=0) is False
    assert item_qty(world, "ember_shard") == 0
    world.ctx.sql.execute(handlers._CREDIT_COINS_EPOCH, ["Tess", 999, UID, 0])
    assert get_player(world)["coins"] == 0

    # while CURRENT-epoch play works normally
    fresh = get_player(world)
    handlers._settle_social(world.ctx, fresh, 30, 0, None)
    assert get_player(world)["xp"] == 30


def test_duel_accept_refuses_cross_epoch_challenge(world):
    # A challenge staked before a Rekindling must dissolve at accept time —
    # its escrow can never ferry value across the burn to either fighter.
    started(world)
    ready_hero(world, "300")
    set_player(world, "300", coins=500)
    world.conn.execute(
        "INSERT INTO duels (id, challenger_id, target_id, challenger_name, bet, "
        "status, created_at, epoch) VALUES ('xe-d', ?, '300', 'Tess', 100, "
        "'open', ?, 0)", [UID, int(world.clock.now())])
    world.conn.commit()
    set_player(world, rekindles=1)  # challenger has since been reborn
    handlers.comp_duel_accept(world.ctx, press(f"{handlers.DUEL_ACCEPT_PREFIX}xe-d", uid="300"))
    assert "burned out" in text_of(last(world.ctx))
    assert get_player(world)["coins"] == 0          # stake stayed burned
    assert get_player(world, "300")["coins"] == 500  # target never debited
    row = world.conn.execute("SELECT status FROM duels WHERE id = 'xe-d'").fetchone()
    assert row[0] == "expired"


def test_trade_accept_refuses_cross_epoch_offer(world):
    started(world)
    ready_hero(world, "300")
    set_player(world, "300", coins=500)
    world.conn.execute(
        "INSERT INTO trades (id, seller_id, buyer_id, seller_name, item_id, qty, "
        "price, status, created_at, epoch) VALUES ('xe-t', ?, '300', 'Tess', "
        "'ember_tonic', 2, 100, 'open', ?, 0)", [UID, int(world.clock.now())])
    world.conn.commit()
    set_player(world, rekindles=1)  # seller has since been reborn
    handlers.comp_trade_accept(world.ctx, press(f"{handlers.TRADE_ACCEPT_PREFIX}xe-t", uid="300"))
    assert "burned out" in text_of(last(world.ctx))
    assert get_player(world, "300")["coins"] == 500            # buyer untouched
    assert item_qty(world, game.LIFE_POTION, "300") == 0       # no goods moved
    assert item_qty(world, game.LIFE_POTION) == 0              # escrow burned
    row = world.conn.execute("SELECT status FROM trades WHERE id = 'xe-t'").fetchone()
    assert row[0] == "expired"


def test_escrow_opened_against_stale_epoch_is_refused(world):
    # A challenge row carrying a pre-burn epoch must refuse refunds post-burn.
    started(world)
    ready_hero(world, "300")
    world.conn.execute(
        "INSERT INTO duels (id, challenger_id, target_id, challenger_name, bet, "
        "status, created_at, epoch) VALUES ('stale-d', ?, '300', 'Tess', 250, "
        "'open', ?, 0)", [UID, int(world.clock.now())])
    world.conn.commit()
    set_player(world, rekindles=1)  # the character has since been reborn
    handlers.comp_duel_decline(world.ctx, press(f"{handlers.DUEL_DECLINE_PREFIX}stale-d", uid="300"))
    assert get_player(world)["coins"] == 0  # the 250-Ember stake stayed burned


def test_no_season_no_banner(world):
    started(world)  # fixture sits on Jan 12 — outside every window
    handlers.cmd_hunt(world.ctx, slash("hunt"))
    assert "creature of the season" not in text_of(last(world.ctx))
    assert get_player(world)["coins"] == 10


# --- Meta: manifest, registration, capabilities, packaging traps -------------------------------------

def test_manifest_matches_handlers_and_dashboard():
    manifest = json.loads((ROOT / "manifest.json").read_text())
    assert [c["name"] for c in manifest["slash_commands"]] == handlers.COMMAND_NAMES
    assert manifest["capabilities_required"] == manifest["capabilities_requested"]
    assert set(manifest["capabilities_required"]) == {
        "interaction:respond", "storage:kv", "storage:sql", "discord:send_message"}

    dash = json.loads((ROOT / "dashboard_manifest.json").read_text())
    declared = set()
    for page in dash["pages"]:
        for widget in page["widgets"]:
            declared.add(widget.get("rpc_method"))
            declared.add(widget.get("save_rpc_method"))
    declared.discard(None)
    assert declared == set(handlers.plugin._dashboard_handlers.keys())


def test_slash_dispatch_through_registered_filters(world):
    registry = handlers.plugin._event_handlers["interaction_create"]
    started(world)
    for command in handlers.COMMAND_NAMES:
        ctx = world.make_ctx()
        options = {"buy": [{"name": "item", "value": "charstick"}],
                   "sell": [{"name": "item", "value": "charstick"}],
                   "coinflip": [{"name": "bet", "value": 50}],
                   "enchant": [{"name": "slot", "value": "sword"}],
                   "duel": [{"name": "user", "value": "42424242"}],
                   "trade": [{"name": "user", "value": "42424242"},
                             {"name": "item", "value": "charstick"},
                             {"name": "price", "value": 10}],
                   "guild": [{"name": "action", "value": "info"}],
                   "equip": [{"name": "item", "value": "ironspark blade"}]}.get(command)
        event = slash(command, options=options)
        for wrapped in registry:
            wrapped(ctx, event)
        assert len(ctx.interaction.responses) == 1, command


def test_component_dispatch_respects_interaction_type(world):
    registry = handlers.plugin._event_handlers["interaction_create"]
    started(world)
    ctx = world.make_ctx()
    event = press(f"{handlers.HUNT_AGAIN_PREFIX}{UID}")
    for wrapped in registry:
        wrapped(ctx, event)
    assert len(ctx.interaction.responses) == 1  # only the hunt_again prefix handler fired


def test_manifest_capabilities_are_sufficient(world):
    caps = json.loads((ROOT / "manifest.json").read_text())["capabilities_required"]
    ctx = world.make_ctx(capabilities=caps)
    handlers.cmd_start(ctx, slash("start", uid="777"))
    handlers.cmd_hunt(ctx, slash("hunt", uid="777"))
    handlers.cmd_daily(ctx, slash("daily", uid="777"))
    handlers.dash_get_settings(ctx, {"discord_srv_id": "999"})
    handlers.dash_save_settings(
        ctx, {"discord_srv_id": "999", "values": {"economy_multiplier": "1", "allowed_channel_id": ""}})
    assert len(ctx.interaction.responses) == 3

    # the dungeon join broadcast exercises discord:send_message under gating
    set_player(world, uid="777", level=3)
    handlers.cmd_dungeon(ctx, slash("dungeon", uid="777", interaction_id="cap-d1"))
    ready_hero(world, "778")
    handlers.comp_dungeon_join(ctx, press(f"{handlers.DUNGEON_JOIN_PREFIX}cap-d1", uid="778"))
    assert ctx.discord.messages_sent and "joined" in ctx.discord.messages_sent[-1]["content"]

    # The recorder ctx.sql (no FakeSql override) exercises the storage:sql capability gate.
    recorder = MockContext(clock=world.clock, capabilities=caps)
    handlers.cmd_top(recorder, slash("top"))
    assert len(recorder.interaction.responses) == 1

    gated = MockContext(clock=world.clock, capabilities=["interaction:respond"])
    with pytest.raises(CapabilityError):
        gated.sql.query("SELECT 1", limit=1)


def test_custom_ids_fit_discord_limit():
    snowflake = "9" * 20
    longest_recipe = max(game.RECIPES, key=len)
    for cid in (f"{handlers.HUNT_AGAIN_PREFIX}{snowflake}",
                f"{handlers.HEAL_PREFIX}{snowflake}",
                f"{handlers.SHOP_PAGE_PREFIX}2:{snowflake}",
                f"{handlers.DUNGEON_JOIN_PREFIX}{snowflake}",
                f"{handlers.DUNGEON_BEGIN_PREFIX}{snowflake}",
                f"{handlers.CRAFT_PREFIX}{longest_recipe}:{snowflake}",
                f"{handlers.DUEL_ACCEPT_PREFIX}{snowflake}",
                f"{handlers.DUEL_DECLINE_PREFIX}{snowflake}",
                f"{handlers.TRADE_ACCEPT_PREFIX}{snowflake}",
                f"{handlers.TRADE_DECLINE_PREFIX}{snowflake}",
                f"{handlers.TRADE_CANCEL_PREFIX}{snowflake}",
                f"{handlers.ARENA_JOIN_PREFIX}2026-06-11",
                f"{handlers.LOOT_ODDS_PREFIX}{snowflake}",
                # production fallback ids are "<uid>-<epoch>" (31 chars)
                f"{handlers.DUNGEON_JOIN_PREFIX}{snowflake}-1781204440",
                f"{handlers.TRADE_CANCEL_PREFIX}{snowflake}-1781204440",
                f"{handlers.DUEL_ACCEPT_PREFIX}{snowflake}-1781204440",
                f"{handlers.QUEST_CLAIM_PREFIX}2:{snowflake}",
                f"{handlers.REKINDLE_CONFIRM_PREFIX}{snowflake}",
                f"{handlers.LOOT_AGAIN_PREFIX}blazing_cache:{snowflake}",
                f"{handlers.PET_SET_PREFIX}ashwhisker:{snowflake}"):
        assert len(cid) <= 100


_HOST_ALLOWED_SQL = ("ALTER TABLE", "CREATE INDEX", "CREATE TABLE", "DELETE",
                     "DROP INDEX", "DROP TABLE", "INSERT", "SELECT", "UPDATE")


def test_all_sql_statement_types_pass_host_allowlist():
    # The live host rejects any statement type outside its allowlist (e.g.
    # CREATE UNIQUE INDEX) — scan every string literal that looks like SQL.
    import ast
    source = (ROOT / "handlers.py").read_text()
    sql_starters = ("SELECT ", "INSERT ", "UPDATE ", "DELETE ", "CREATE ", "ALTER ",
                    "DROP ", "WITH ", "TRUNCATE ", "REPLACE ", "EXPLAIN ", "VACUUM ",
                    "GRANT ", "BEGIN ", "MERGE ")
    checked = 0
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            text = node.value.strip()
            # case-sensitive + trailing space: real SQL constants are
            # uppercase keywords, unlike dict keys such as "drop".
            if text.startswith(sql_starters):
                assert text.startswith(_HOST_ALLOWED_SQL), text[:60]
                checked += 1
    assert checked > 40  # sanity: the scan actually saw the statement constants


def test_options_accept_live_gateway_alias(world):
    # The live gateway carries both `options` and the legacy `command_options`;
    # if `options` ever drops, arguments must still parse from the alias.
    started(world)
    set_player(world, coins=100)
    event = make_event("interaction_create", command_name="coinflip", user_id=UID,
                       channel_id="1", user_username="cubcadetxt1",
                       command_options=[{"name": "bet", "type": 4, "value": 10}])
    handlers.cmd_coinflip(world.ctx, event)
    assert get_player(world)["coins"] == 110  # bet parsed, ScriptRng(0.0) wins


def test_username_accepts_live_gateway_field(world):
    # Production sends user_username (not the documented user_name).
    event = make_event("interaction_create", command_name="start", user_id="808",
                       channel_id="1", user_username="livename")
    handlers.cmd_start(world.ctx, event)
    assert get_player(world, "808")["username"] == "livename"


def test_no_message_content_token_in_shipped_source():
    # The platform validator demands events:message_content if any shipped .py
    # contains a ("content" / ('content' token; we must never trip it.
    for name in ("handlers.py", "game.py", "__main__.py"):
        source = (ROOT / name).read_text()
        assert '("content' not in source and "('content" not in source, name


def test_handler_errors_are_walled(world, monkeypatch):
    started(world)

    def explode(*args, **kwargs):
        raise RuntimeError("RPC error (sql.execute): boom")

    monkeypatch.setattr(world.ctx.sql, "query_one", explode)
    handlers.cmd_profile(world.ctx, slash("profile"))
    response = last(world.ctx)
    assert response["ephemeral"] is True and "went wrong" in text_of(response)
    assert any(e["level"] == "error" for e in world.ctx.log_entries)


# --- Regression-audit additions: dispatch, ambiguity, epoch survivals, hooks ---

def test_component_dispatch_through_registered_filters_all_prefixes(world):
    """Every component prefix must be wired into the interaction registry and
    produce exactly one response when dispatched — even if it's a rejection."""
    registry = handlers.plugin._event_handlers["interaction_create"]
    started(world)
    cases = [
        f"{handlers.HUNT_AGAIN_PREFIX}{UID}",
        f"{handlers.HEAL_PREFIX}{UID}",
        f"{handlers.SHOP_PAGE_PREFIX}1:{UID}",
        f"{handlers.DUNGEON_JOIN_PREFIX}nope",
        f"{handlers.DUNGEON_BEGIN_PREFIX}nope",
        f"{handlers.CRAFT_PREFIX}{game.LIFE_POTION}:{UID}",
        f"{handlers.DUEL_ACCEPT_PREFIX}nope",
        f"{handlers.DUEL_DECLINE_PREFIX}nope",
        f"{handlers.TRADE_ACCEPT_PREFIX}nope",
        f"{handlers.TRADE_DECLINE_PREFIX}nope",
        f"{handlers.TRADE_CANCEL_PREFIX}nope",
        f"{handlers.ARENA_JOIN_PREFIX}1969-01-01",
        f"{handlers.QUEST_CLAIM_PREFIX}0:{UID}",
        f"{handlers.REKINDLE_CONFIRM_PREFIX}{UID}",
        f"{handlers.LOOT_AGAIN_PREFIX}ember_cache:{UID}",
        f"{handlers.LOOT_ODDS_PREFIX}{UID}",
        f"{handlers.PET_SET_PREFIX}cinderpup:{UID}",
    ]
    for custom_id in cases:
        ctx = world.make_ctx()
        event = press(custom_id)
        for wrapped in registry:
            wrapped(ctx, event)
        assert len(ctx.interaction.responses) == 1, custom_id


def test_duel_open_ambiguity_reread_keeps_committed_challenge(world, monkeypatch):
    """If the open INSERT raises but actually committed, the challenge must be
    kept (no refund — a later expiry would otherwise pay the stake twice)."""
    started(world)
    ready_hero(world, "300")
    set_player(world, coins=100)
    real_execute = world.ctx.sql.execute

    def ambiguous(sql, params=None):
        result = real_execute(sql, params)
        if sql.startswith("INSERT INTO duels"):
            raise RuntimeError("RPC error (sql.execute): timeout after commit")
        return result

    monkeypatch.setattr(world.ctx.sql, "execute", ambiguous)
    handlers.cmd_duel(world.ctx, slash(
        "duel", options=[{"name": "user", "value": "300"}, {"name": "bet", "value": 60}],
        interaction_id="amb-d"))
    row = world.conn.execute("SELECT status FROM duels WHERE id = 'amb-d'").fetchone()
    assert row[0] == "open"                      # challenge kept
    assert get_player(world)["coins"] == 40      # stake stays escrowed, no refund
    assert "challenges" in text_of(last(world.ctx))
    lock = world.conn.execute(
        "SELECT ref FROM locks WHERE name = ?", [f"duel:{UID}"]).fetchone()
    assert lock[0] == "amb-d"                    # lock still held by the live row


def test_duel_open_ambiguity_reread_rejects_foreign_row(world, monkeypatch):
    """If the re-read finds a row with the SAME id but a DIFFERENT owner (id
    collision), it must refund and never claim someone else's challenge."""
    started(world)
    ready_hero(world, "300")
    set_player(world, coins=100)
    world.conn.execute(
        "INSERT INTO duels (id, challenger_id, target_id, challenger_name, bet, "
        "status, created_at, epoch) VALUES ('amb-f', '999', '300', 'Else', 5, "
        "'open', ?, 0)", [int(world.clock.now())])
    world.conn.commit()
    real_execute = world.ctx.sql.execute

    def ambiguous(sql, params=None):
        if sql.startswith("INSERT INTO duels"):
            raise RuntimeError("RPC error (sql.execute): boom")
        return real_execute(sql, params)

    monkeypatch.setattr(world.ctx.sql, "execute", ambiguous)
    handlers.cmd_duel(world.ctx, slash(
        "duel", options=[{"name": "user", "value": "300"}, {"name": "bet", "value": 60}],
        interaction_id="amb-f"))
    assert get_player(world)["coins"] == 100     # refunded, nothing claimed
    assert "flames shifted" in text_of(last(world.ctx))


def test_earned_income_survives_rekindle_by_design(world):
    """Arena prizes and completed-quest claims are income already earned —
    both deliberately cross the epoch boundary (documented on the confirm)."""
    # quest claim after rekindle
    started(world)
    set_player(world, level=22, coins=0)
    day = game.arena_day(int(world.clock.now()))
    quest = game.daily_quests(UID, day)[0]
    for _ in range(quest["target"]):
        handlers._quest_event(world.ctx, UID, quest["key"], int(world.clock.now()))
    handlers.cmd_rekindle(world.ctx, slash("rekindle"))
    handlers.comp_rekindle_confirm(world.ctx, press(f"{handlers.REKINDLE_CONFIRM_PREFIX}{UID}"))
    assert get_player(world)["rekindles"] == 1 and get_player(world)["coins"] == 0
    handlers.comp_quest_claim(world.ctx, press(f"{handlers.QUEST_CLAIM_PREFIX}0:{UID}"))
    expected = game.quest_reward(quest, 1, game.player_boosts("", 1, None))
    assert get_player(world)["coins"] == expected  # claim pays at the NEW epoch

    # arena prize paid out to a since-rekindled entrant
    ready_hero(world, "A1", level=22, name="Aria")
    yesterday = game.arena_day(int(world.clock.now()))
    world.conn.execute(
        "INSERT INTO arena_entries (day, user_id, username, score, paid_fee, "
        "channel_id, created_at) VALUES (?, 'A1', 'Aria', 42, ?, '1', ?)",
        [yesterday, game.ARENA_FEE, int(world.clock.now())])
    world.conn.execute("UPDATE players SET rekindles = 1 WHERE user_id = 'A1'")
    world.conn.commit()
    world.clock.advance(86400)
    started(world)  # idempotent; gives the sweep a caller
    set_player(world, coins=100)
    handlers.cmd_arena(world.ctx, slash("arena"))
    # pool = 1×50 fee + 100 bonus = 150 → 1st place 50% = 75, paid to the
    # rekindled (epoch 1) character: earned income crosses the burn by design
    assert get_player(world, "A1")["coins"] == 75


def test_quest_hooks_fire_through_host_commands(world):
    """Drive each hook through its real host command for a hero whose daily
    board contains that key, then assert the progress row ticked."""
    now = int(world.clock.now())
    day = game.arena_day(now)

    def uid_with(key, start=2000):
        return next(str(n) for n in range(start, start + 400)
                    if key in [q["key"] for q in game.daily_quests(str(n), day)])

    def progress(uid, key):
        idx = [q["key"] for q in game.daily_quests(uid, day)].index(key)
        row = world.conn.execute(
            "SELECT progress FROM quest_progress WHERE user_id = ? AND day = ? "
            "AND quest_idx = ?", [uid, day, idx]).fetchone()
        return row[0] if row else 0

    # hunt_win
    uid = uid_with("hunt_win")
    ready_hero(world, uid, level=1)
    handlers.cmd_hunt(world.ctx, slash("hunt", uid=uid))
    assert progress(uid, "hunt_win") == 1
    # adventure_win
    uid = uid_with("adventure_win")
    ready_hero(world, uid, level=1)
    handlers.cmd_adventure(world.ctx, slash("adventure", uid=uid))
    assert progress(uid, "adventure_win") == 1
    # heal (auto-buy path)
    uid = uid_with("heal")
    ready_hero(world, uid, level=1)
    set_player(world, uid=uid, hp=40, coins=100, last_action_at=now)
    handlers.cmd_heal(world.ctx, slash("heal", uid=uid))
    assert progress(uid, "heal") == 1
    # craft
    uid = uid_with("craft")
    ready_hero(world, uid, level=1)
    set_player(world, uid=uid, coins=100)
    give_items(world, uid, ember_shard=1)
    handlers.cmd_craft(world.ctx, slash("craft", uid=uid,
                                        options=[{"name": "item", "value": "ember tonic"}]))
    assert progress(uid, "craft") == 1
    # coinflip
    uid = uid_with("coinflip")
    ready_hero(world, uid, level=1)
    set_player(world, uid=uid, coins=100)
    handlers.cmd_coinflip(world.ctx, slash("coinflip", uid=uid,
                                           options=[{"name": "bet", "value": 10}]))
    assert progress(uid, "coinflip") == 1
    # arena
    uid = uid_with("arena")
    ready_hero(world, uid, level=1)
    set_player(world, uid=uid, coins=100)
    handlers.cmd_arena(world.ctx, slash("arena", uid=uid))
    assert progress(uid, "arena") == 1
    # duel — both fighters tick; pick a challenger whose board has it
    uid = uid_with("duel")
    ready_hero(world, uid, level=1)
    ready_hero(world, "9901", level=1)
    set_player(world, uid=uid, coins=50)
    set_player(world, uid="9901", coins=50)
    handlers.cmd_duel(world.ctx, slash(
        "duel", uid=uid, options=[{"name": "user", "value": "9901"}],
        interaction_id=f"qd-{uid}"))
    handlers.comp_duel_accept(world.ctx, press(f"{handlers.DUEL_ACCEPT_PREFIX}qd-{uid}", uid="9901"))
    assert progress(uid, "duel") == 1
    # dungeon — resolve a two-hero expedition including the tracked hero
    uid = uid_with("dungeon")
    ready_hero(world, uid, level=3)
    ready_hero(world, "9902", level=3)
    handlers.cmd_dungeon(world.ctx, slash("dungeon", uid=uid, interaction_id=f"qg-{uid}"))
    handlers.comp_dungeon_join(world.ctx, press(f"{handlers.DUNGEON_JOIN_PREFIX}qg-{uid}", uid="9902"))
    handlers.comp_dungeon_begin(world.ctx, press(f"{handlers.DUNGEON_BEGIN_PREFIX}qg-{uid}", uid=uid))
    assert progress(uid, "dungeon") == 1


def test_lock_self_heal_age_branch_and_prune(world):
    now = int(world.clock.now())
    # a lock whose holder row is still 'open' but far beyond the domain TTL
    # is breakable via the age branch
    world.conn.execute(
        "INSERT INTO duels (id, challenger_id, target_id, bet, status, created_at, epoch) "
        "VALUES ('aged', '1', '2', 0, 'open', ?, 0)", [now])
    world.conn.execute(
        "INSERT INTO locks (name, ref, created_at) VALUES ('duel:1', 'aged', ?)",
        [now - game.DUEL_TTL - 700])
    world.conn.commit()
    assert handlers._acquire_lock(world.ctx, "duel", "duel:1", "newref", now) is True
    # ancient lock rows are pruned by the duel sweep
    world.conn.execute(
        "INSERT INTO locks (name, ref, created_at) VALUES ('trade:dead', 'x', ?)",
        [now - 8 * 86400])
    world.conn.commit()
    handlers._sweep_stale_duels(world.ctx, now)
    assert world.conn.execute(
        "SELECT COUNT(*) FROM locks WHERE name = 'trade:dead'").fetchone()[0] == 0


def test_craft_shortage_refund_burns_across_epoch(world, monkeypatch):
    """The shortage refund runs with the epoch captured at _begin: a Rekindle
    landing mid-craft burns the fee instead of resurrecting it."""
    started(world)
    set_player(world, coins=200)
    give_items(world, UID, ember_shard=2)  # phoenix draught also needs wraithsilk
    real_take = handlers._take_item

    def take_then_rekindle(ctx, user_id, item_id):
        world.conn.execute("UPDATE players SET rekindles = 1, coins = 0 WHERE user_id = ?",
                           [UID])
        world.conn.commit()
        return real_take(ctx, user_id, item_id)

    monkeypatch.setattr(handlers, "_take_item", take_then_rekindle)
    handlers.cmd_craft(world.ctx, slash("craft",
                                        options=[{"name": "item", "value": "Phoenix Draught"}]))
    assert "Missing materials" in text_of(last(world.ctx))
    assert get_player(world)["coins"] == 0          # fee refund burned (epoch 0 != 1)


def test_dashboard_phase_cards_and_drift_guard(world):
    params = {"discord_srv_id": "999"}
    for handler in (handlers.dash_dungeons_cleared, handlers.dash_duels_fought,
                    handlers.dash_caches_opened):
        out = handler(world.ctx, params)
        assert isinstance(out["value"], int) and "change" in out
    # the committed dashboard JSON must match what the generator produces
    import runpy
    before = (ROOT / "dashboard_manifest.json").read_text()
    runpy.run_path(str(ROOT / "tests" / "gen_dashboard.py"))
    after = (ROOT / "dashboard_manifest.json").read_text()
    assert before == after, "dashboard_manifest.json drifted from its generator"


def test_open_odds_button_and_arg(world):
    started(world)
    give_items(world, UID, ember_cache=1)
    handlers.cmd_open(world.ctx, slash("open", options=[{"name": "item", "value": "odds"}]))
    assert "what's inside?" in text_of(last(world.ctx))   # odds WITHOUT spending
    assert item_qty(world, "ember_cache") == 1
    handlers.cmd_open(world.ctx, slash("open"))
    buttons = last(world.ctx)["components"][0].to_dict()["components"]
    odds_id = f"{handlers.LOOT_ODDS_PREFIX}{UID}"
    assert any(b["custom_id"] == odds_id for b in buttons)
    handlers.comp_loot_odds(world.ctx, press(odds_id, uid="999"))
    assert "your own odds" in text_of(last(world.ctx))
    handlers.comp_loot_odds(world.ctx, press(odds_id))
    assert "JACKPOT" in text_of(last(world.ctx))


def test_adventure_copy_has_no_doubled_article(world):
    started(world)
    handlers.cmd_adventure(world.ctx, slash("adventure"))
    body = text_of(last(world.ctx))
    assert "the the" not in body.lower()
    assert "the Smoldering Fen" in body


def test_coinflip_is_debit_first(world):
    """The stake must leave the balance even when the flip is a win — the
    recorder shows debit-then-credit, never credit-only."""
    started(world)
    set_player(world, coins=100)
    handlers.cmd_coinflip(world.ctx, slash("coinflip", options=[{"name": "bet", "value": 60}]))
    flips = [e for e in world.ctx.sql.executed
             if isinstance(e.get("sql"), str) and "coins = coins" in e["sql"]]
    assert any("coins - " in e["sql"] for e in flips), "no debit recorded"
    assert get_player(world)["coins"] == 160  # ScriptRng(0.0) wins: 100-60+120


# --- DDL rate limit (live host: "max 5 DDL statements per hour") --------------

def test_bootstrap_marks_revision_and_goes_zero_ddl(world):
    """A clean bootstrap records the schema revision in KV; every later boot
    must issue ZERO DDL (steady-state starts can't burn the hourly budget)."""
    assert world.ctx.kv.get(handlers.KV_SCHEMA_REV) == handlers._SCHEMA_REV
    ctx2 = world.make_ctx()
    # production KV is durable per (server, plugin) and shared across worker
    # processes; MockContext KV is per-instance, so carry the marker over
    ctx2.kv.set(handlers.KV_SCHEMA_REV, world.ctx.kv.get(handlers.KV_SCHEMA_REV))
    handlers.on_ready(ctx2)
    assert [e for e in ctx2.sql.executed
            if str(e.get("sql", "")).startswith(("CREATE", "ALTER"))] == []


def test_rate_limited_bootstrap_retries_and_heals(world, monkeypatch):
    """Statements rejected by the DDL budget defer the revision marker; the
    per-command retry re-runs the bootstrap after the window and heals."""
    world.ctx.kv.delete(handlers.KV_SCHEMA_REV)
    world.conn.execute("DROP TABLE quest_progress")
    world.conn.commit()
    real_execute = world.ctx.sql.execute
    limited = {"on": True}

    def rate_limited(sql, params=None):
        if limited["on"] and sql.startswith("CREATE TABLE IF NOT EXISTS quest_progress"):
            raise RuntimeError("RPC error (sql.execute): DDL rate limit: "
                               "max 5 DDL statements per hour")
        return real_execute(sql, params)

    monkeypatch.setattr(world.ctx.sql, "execute", rate_limited)
    assert handlers._ensure_schema(world.ctx, "srv1") is False
    assert world.ctx.kv.get(handlers.KV_SCHEMA_REV) is None  # NOT marked done
    assert "srv1" not in handlers._schema_ok

    # while the table is missing, the user sees the settling-in message
    started(world)
    handlers.cmd_questlog(world.ctx, slash("questlog", guild_id="srv1"))
    assert "still settling in" in text_of(last(world.ctx))

    # the budget refills: the next command past the retry window heals it
    limited["on"] = False
    world.clock.advance(handlers._SCHEMA_RETRY_SECONDS + 1)
    handlers.cmd_questlog(world.ctx, slash("questlog", guild_id="srv1"))
    assert "Quest Board" in text_of(last(world.ctx))
    assert world.ctx.kv.get(handlers.KV_SCHEMA_REV) == handlers._SCHEMA_REV
    assert "srv1" in handlers._schema_ok


def test_schema_orders_core_tables_into_first_ddl_window(world):
    """Fresh installs land ~5 tables per hourly window: the core loop, quest
    bookkeeping, and the locks gate must be in the first five statements."""
    first_window = " ".join(handlers._SCHEMA[:5])
    for table in ("players", "inventory", "quest_progress", "locks"):
        assert "EXISTS " + table in first_window, table


def test_provisioned_server_reaches_marker_with_zero_ddl(world):
    """The 0.4.3 flaw: no-op ALTERs count against the host's DDL budget, so a
    marker that required all 20 statements to SUCCEED in one pass could never
    be reached (8 ALTERs > 5/hour). Probes must prove no-ops instead: a fully
    provisioned server with no marker sets it while issuing ZERO DDL."""
    world.ctx.kv.delete(handlers.KV_SCHEMA_REV)
    handlers._schema_ok.clear()
    ctx2 = world.make_ctx()
    assert handlers._ensure_schema(ctx2, "srv-up") is True
    assert [e for e in ctx2.sql.executed
            if str(e.get("sql", "")).startswith(("CREATE", "ALTER"))] == []
    assert ctx2.kv.get(handlers.KV_SCHEMA_REV) == handlers._SCHEMA_REV
    assert "srv-up" in handlers._schema_ok


def test_probe_gating_issues_only_missing_ddl(world):
    """Partial provisioning: only the statements whose probes fail are sent —
    the hourly budget is never spent re-asserting what already exists."""
    world.ctx.kv.delete(handlers.KV_SCHEMA_REV)
    handlers._schema_ok.clear()
    world.conn.execute("DROP TABLE locks")
    world.conn.commit()
    ctx2 = world.make_ctx()
    assert handlers._ensure_schema(ctx2, "srv-p") is True
    ddl = [str(e["sql"]) for e in ctx2.sql.executed
           if str(e.get("sql", "")).startswith(("CREATE", "ALTER"))]
    assert len(ddl) == 1 and "locks" in ddl[0]
    assert world.conn.execute("SELECT COUNT(*) FROM locks").fetchone()[0] == 0
    assert ctx2.kv.get(handlers.KV_SCHEMA_REV) == handlers._SCHEMA_REV


def test_schema_probes_pair_with_their_ddl():
    """Meta: every probe targets the relation its paired DDL creates/alters."""
    assert len(handlers._SCHEMA) == len(handlers._SCHEMA_PROBES)
    assert len(handlers._MIGRATIONS) == len(handlers._MIGRATION_PROBES)
    for probe, ddl in zip(handlers._SCHEMA_PROBES, handlers._SCHEMA):
        table = probe.split(" FROM ")[1].split(" ")[0]
        assert "EXISTS " + table in ddl, (probe, ddl)
    for probe, ddl in zip(handlers._MIGRATION_PROBES, handlers._MIGRATIONS):
        column = probe.split("SELECT ")[1].split(" ")[0]
        table = probe.split(" FROM ")[1].split(" ")[0]
        assert "ALTER TABLE " + table in ddl and "EXISTS " + column in ddl, (probe, ddl)


# --- SDK 0.7+ in-place UX (respond(update_message=True)) + coverage gaps ------------------------
# The pin moved to yourbot-sdk>=0.8.2, which unlocks respond(update_message=True):
# component handlers now edit their attached card in place (stripping dead
# buttons / refreshing lists) while slash commands still post fresh cards. These
# tests pin that behavior, plus two previously-unasserted branches.

def _in_place(response):
    return response.get("update_message") is True


def _buttons_cleared(response):
    return not (response.get("components") or [])


def test_profile_of_started_other_hero_renders(world):
    started(world)
    ready_hero(world, "300", level=4, name="Borin")
    handlers.cmd_profile(world.ctx, slash("profile", options=[{"name": "user", "value": "300"}]))
    body = text_of(last(world.ctx))
    assert "Borin — Level 4" in body
    assert "█" in body or "░" in body            # HP/XP bars rendered for the target
    assert "Fists" in body                         # equipped gear shown


def test_leaderboard_unknown_metric_falls_back_to_level(world):
    _seed_heroes(world)
    handlers.cmd_top(world.ctx, slash("top",
                                              options=[{"name": "metric", "value": "banana"}]))
    body = text_of(last(world.ctx))
    assert "by level" in body                      # unrecognized metric coerced to level
    assert body.index("**Borin**") < body.index("**Cinder**") < body.index("**Aria**")


def test_shop_pagination_edits_in_place_slash_stays_fresh(world):
    handlers.cmd_shop(world.ctx, slash("shop"))
    assert not last(world.ctx).get("update_message")       # first /shop is a fresh card
    handlers.comp_shop_page(world.ctx, press(f"{handlers.SHOP_PAGE_PREFIX}1:{UID}"))
    turned = last(world.ctx)
    assert turned["update_message"] is True and "Armor" in text_of(turned)


def test_hunt_again_edits_combat_card_in_place(world):
    started(world)
    handlers.cmd_hunt(world.ctx, slash("hunt"))
    assert not last(world.ctx).get("update_message")       # /hunt posts a fresh card
    world.clock.advance(game.HUNT_COOLDOWN + 1)
    handlers.comp_hunt_again(world.ctx, press(f"{handlers.HUNT_AGAIN_PREFIX}{UID}"))
    refreshed = last(world.ctx)
    assert refreshed["update_message"] is True              # the button refreshes it in place
    # ...and the card body is genuinely re-rendered (not blanked): encounter
    # stats plus the live "Hunt again" button are present.
    assert "HP" in text_of(refreshed)
    ids = [c["custom_id"] for c in refreshed["components"][0].to_dict()["components"]]
    assert any(i.startswith(handlers.HUNT_AGAIN_PREFIX) for i in ids)


def test_duel_settlement_rewrites_card_and_strips_buttons(world):
    duel_setup(world, bet=50)
    assert not last(world.ctx).get("update_message")       # challenge card carries live buttons
    handlers.comp_duel_accept(world.ctx, press(f"{handlers.DUEL_ACCEPT_PREFIX}duel1", uid="300"))
    settled = last(world.ctx)
    assert "defeats" in text_of(settled)
    assert _in_place(settled) and _buttons_cleared(settled)


def test_duel_decline_strips_buttons(world):
    duel_setup(world, bet=40)
    handlers.comp_duel_decline(world.ctx, press(f"{handlers.DUEL_DECLINE_PREFIX}duel1", uid="300"))
    declined = last(world.ctx)
    assert "declined" in text_of(declined).lower()
    assert _in_place(declined) and _buttons_cleared(declined)
    assert get_player(world)["coins"] == 100               # escrow still refunded


def test_trade_accept_rewrites_card_and_strips_buttons(world):
    trade_setup(world)
    handlers.comp_trade_accept(world.ctx, press(f"{handlers.TRADE_ACCEPT_PREFIX}trade1", uid="300"))
    struck = last(world.ctx)
    assert "Deal struck" in text_of(struck)
    assert _in_place(struck) and _buttons_cleared(struck)


def test_trade_decline_and_cancel_strip_buttons(world):
    trade_setup(world)
    handlers.comp_trade_decline(world.ctx, press(f"{handlers.TRADE_DECLINE_PREFIX}trade1", uid="300"))
    declined = last(world.ctx)
    assert _in_place(declined) and _buttons_cleared(declined)
    assert item_qty(world, game.LIFE_POTION) == 3          # escrow still refunded
    # a fresh offer, this time withdrawn by the seller
    handlers.cmd_trade(world.ctx, slash(
        "trade", options=[{"name": "user", "value": "300"},
                          {"name": "item", "value": "ember tonic"},
                          {"name": "price", "value": 10}], interaction_id="trade2"))
    handlers.comp_trade_cancel(world.ctx, press(f"{handlers.TRADE_CANCEL_PREFIX}trade2", uid=UID))
    cancelled = last(world.ctx)
    assert _in_place(cancelled) and _buttons_cleared(cancelled)


def test_loot_reroll_edits_card_in_place_slash_stays_fresh(world):
    started(world)
    give_items(world, UID, ember_cache=2)
    handlers.cmd_open(world.ctx, slash("open"))
    assert not last(world.ctx).get("update_message")       # /open posts a fresh card
    handlers.comp_loot_again(world.ctx, press(f"{handlers.LOOT_AGAIN_PREFIX}ember_cache:{UID}"))
    rerolled = last(world.ctx)
    # a common (ephemeral) outcome refreshes the same card in place
    assert rerolled["update_message"] is True and rerolled["ephemeral"] is True


def test_loot_reroll_public_jackpot_posts_fresh_card(world, monkeypatch):
    # A public reveal can't be delivered by editing the ephemeral card the
    # button lives on — a jackpot from "Open another" must post a fresh
    # public message, never an in-place edit.
    started(world)
    give_items(world, UID, ember_cache=1)
    monkeypatch.setattr(handlers, "_rng", SeqRng([0.995]))
    handlers.comp_loot_again(world.ctx, press(f"{handlers.LOOT_AGAIN_PREFIX}ember_cache:{UID}"))
    reveal = last(world.ctx)
    assert "JACKPOT" in text_of(reveal)
    assert reveal["ephemeral"] is False
    assert reveal["update_message"] is False


def test_loot_reroll_public_first_hatch_posts_fresh_card(world, monkeypatch):
    started(world)
    give_items(world, UID, ember_cache=1)
    monkeypatch.setattr(handlers, "_rng", SeqRng([0.93]))
    handlers.comp_loot_again(world.ctx, press(f"{handlers.LOOT_AGAIN_PREFIX}ember_cache:{UID}"))
    reveal = last(world.ctx)
    assert get_player(world)["pet"] == "ashwhisker"        # first hatch auto-adopts
    assert reveal["ephemeral"] is False
    assert reveal["update_message"] is False


def test_craft_button_rerenders_recipe_list_in_place(world):
    started(world)
    give_items(world, UID, ember_shard=1)
    set_player(world, coins=100)
    handlers.comp_craft(world.ctx, press(f"{handlers.CRAFT_PREFIX}{game.LIFE_POTION}:{UID}"))
    board = last(world.ctx)
    assert item_qty(world, game.LIFE_POTION) == 1
    assert board["update_message"] is True                 # recipe list refreshed in place
    body = text_of(board)
    assert "Forged" in body and "Crafting" in body         # forge banner + the list


def test_pet_button_rerenders_list_in_place(world):
    started(world)
    give_items(world, UID, cinderpup=1, wisplight=1)
    handlers.cmd_pet(world.ctx, slash("pet"))
    assert not last(world.ctx).get("update_message")       # /pet posts a fresh list
    handlers.comp_pet_set(world.ctx, press(f"{handlers.PET_SET_PREFIX}cinderpup:{UID}"))
    board = last(world.ctx)
    assert get_player(world)["pet"] == "cinderpup"
    assert board["update_message"] is True
    body = text_of(board)
    assert "takes the lead" in body and "Companions" in body


def test_quest_claim_rerenders_board_in_place(world):
    started(world)
    day = game.arena_day(int(world.clock.now()))
    quest = game.daily_quests(UID, day)[0]
    for _ in range(quest["target"]):
        handlers._quest_event(world.ctx, UID, quest["key"], int(world.clock.now()))
    handlers.cmd_questlog(world.ctx, slash("questlog"))
    claim_id = f"{handlers.QUEST_CLAIM_PREFIX}0:{UID}"
    handlers.comp_quest_claim(world.ctx, press(claim_id))
    board = last(world.ctx)
    assert board["update_message"] is True                 # board re-rendered in place
    body = text_of(board)
    assert "Claimed" in body and "Quest Board" in body     # payout banner + the board
    remaining = ([c["custom_id"] for c in board["components"][0].to_dict()["components"]]
                 if board.get("components") else [])
    assert claim_id not in remaining                        # the claimed quest's button dropped


def test_arena_midnight_toctou_counts_entry_and_never_mints(world, monkeypatch):
    """A payout that settles the day in the instant our entry lands must not let
    the fee be refunded on top (the entry was already counted): burn-bias holds."""
    started(world)
    set_player(world, coins=100)
    today = game.arena_day(int(world.clock.now()))
    real_execute = world.ctx.sql.execute

    def racing_execute(sql, params=None):
        rc = real_execute(sql, params)
        # Simulate a concurrent settle claiming today the instant our guarded
        # INSERT commits (arena_days appears AFTER, so our entry was counted).
        if sql == handlers._ARENA_ENTER and rc:
            world.conn.execute(
                "INSERT INTO arena_days (day, resolved_at) VALUES (?, ?) "
                "ON CONFLICT (day) DO NOTHING", [today, int(world.clock.now())])
            world.conn.commit()
        return rc

    monkeypatch.setattr(world.ctx.sql, "execute", racing_execute)
    handlers.cmd_arena(world.ctx, slash("arena"))
    # The entry stands in the (now settled) tournament, counted...
    assert world.conn.execute(
        "SELECT COUNT(*) FROM arena_entries WHERE day = ? AND user_id = ?",
        [today, UID]).fetchone()[0] == 1
    # ...and the fee is NOT refunded on top — the old code deleted + refunded,
    # which would have double-paid a podium finisher.
    assert get_player(world)["coins"] == 100 - game.ARENA_FEE
    assert "final results" in text_of(last(world.ctx))
