"""EmberQuest handlers — slash commands, components, lifecycle, dashboard.

Importable without side effects (plugin.run() lives in __main__.py) so tests
can call every handler directly.

Conventions enforced throughout:
- SQL uses %s placeholders with a params list (psycopg style per SDK 0.6.1);
  statements are always whole literals, never assembled from variables.
- The SDK dispatches events on multiple threads, so every gate is an atomic
  conditional UPDATE judged by its rowcount (cooldown claims, inventory
  takes, coin debits), and balance math is relative (coins = coins + n),
  clamped in SQL — never an absolute value computed from a stale read.
- Order of effects is gate/debit first, grants after: a mid-handler RPC
  failure may cost the player a reward (they can ask again) but can never
  mint coins or items. Failed events are not redelivered by the platform.
- SQL timestamps are the source of truth for all cooldowns; ctx.ephemeral is
  only a spam guard (eviction must never grant a free reset).
- Every interaction path responds exactly once, within the 3s window.
"""

from __future__ import annotations

import functools
import random
import time

from yourbot_sdk import ActionRow, Button, Plugin, RateLimitError, SdkError

import game

plugin = Plugin()

EMBED_COLOR = 0x00BCD4  # cyan accent (gaming-plugin convention)

# custom_id scheme: v1:<domain>:<action>[:<extra>]:<owner_or_target_id>  (kept
# well under the 100-char cap — the SDK silently truncates, which would break
# the routing). Dungeon ids are deliberately NOT owner-scoped: anyone may join.
HUNT_AGAIN_PREFIX = "v1:combat:hunt_again:"
HEAL_PREFIX = "v1:combat:heal:"
SHOP_PAGE_PREFIX = "v1:shop:page:"
DUNGEON_JOIN_PREFIX = "v1:dungeon:join:"
DUNGEON_BEGIN_PREFIX = "v1:dungeon:begin:"
CRAFT_PREFIX = "v1:craft:make:"
DUEL_ACCEPT_PREFIX = "v1:duel:accept:"
DUEL_DECLINE_PREFIX = "v1:duel:decline:"
TRADE_ACCEPT_PREFIX = "v1:trade:accept:"
TRADE_DECLINE_PREFIX = "v1:trade:decline:"
TRADE_CANCEL_PREFIX = "v1:trade:cancel:"
ARENA_JOIN_PREFIX = "v1:arena:join:"
QUEST_CLAIM_PREFIX = "v1:quest:claim:"
REKINDLE_CONFIRM_PREFIX = "v1:prestige:confirm:"
LOOT_AGAIN_PREFIX = "v1:loot:again:"
LOOT_ODDS_PREFIX = "v1:loot:odds:"
PET_SET_PREFIX = "v1:pet:set:"

KV_MULTIPLIER = "economy_multiplier"
KV_CHANNEL = "allowed_channel_id"
KV_SCHEMA_REV = "schema_rev"

COMMAND_NAMES = [
    "start", "profile", "hunt", "adventure", "heal", "inventory",
    "shop", "buy", "sell", "daily", "leaderboard", "coinflip",
    "dungeon", "craft", "enchant",
    "duel", "trade", "guild", "arena",
    "equip", "pet", "open", "quests", "rekindle",
]

_rng = random.Random()


def _now() -> int:
    return int(time.time())


# --- Schema & lifecycle ------------------------------------------------------

_SCHEMA = (
    # ORDER MATTERS: the live host rate-limits schema changes ("DDL rate
    # limit: max 5 DDL statements per hour", verified in production), so a
    # fresh install provisions over several hourly windows. Tables are listed
    # by gameplay priority: the core loop (players/inventory), quest
    # bookkeeping (hunts tick quests passively), and the locks table (which
    # gates duels/trades/dungeons) land in the first window.
    "CREATE TABLE IF NOT EXISTS players ("
    "  user_id           TEXT PRIMARY KEY,"
    "  username          TEXT NOT NULL DEFAULT '',"
    "  level             INT NOT NULL DEFAULT 1,"
    "  xp                BIGINT NOT NULL DEFAULT 0,"
    "  hp                INT NOT NULL DEFAULT 100,"
    "  max_hp            INT NOT NULL DEFAULT 100,"
    "  coins             BIGINT NOT NULL DEFAULT 0,"
    "  sword             TEXT NOT NULL DEFAULT 'fists',"
    "  armor             TEXT NOT NULL DEFAULT 'cloth',"
    "  created_at        BIGINT NOT NULL,"
    "  last_action_at    BIGINT NOT NULL DEFAULT 0,"
    "  last_hunt_at      BIGINT NOT NULL DEFAULT 0,"
    "  last_adventure_at BIGINT NOT NULL DEFAULT 0,"
    "  last_daily_at     BIGINT NOT NULL DEFAULT 0,"
    "  last_dungeon_at   BIGINT NOT NULL DEFAULT 0,"
    "  sword_enchant     INT NOT NULL DEFAULT 0,"
    "  armor_enchant     INT NOT NULL DEFAULT 0,"
    "  last_duel_at      BIGINT NOT NULL DEFAULT 0,"
    "  pet               TEXT NOT NULL DEFAULT '',"
    "  rekindles         INT NOT NULL DEFAULT 0)",
    "CREATE TABLE IF NOT EXISTS inventory ("
    "  user_id TEXT NOT NULL,"
    "  item_id TEXT NOT NULL,"
    "  qty     INT NOT NULL DEFAULT 0,"
    "  PRIMARY KEY (user_id, item_id))",
    # Daily quest progress. The quest LIST is derived deterministically from
    # (day, user_id) — only counters and claim flags need storage.
    "CREATE TABLE IF NOT EXISTS quest_progress ("
    "  user_id   TEXT NOT NULL,"
    "  day       TEXT NOT NULL,"
    "  quest_idx INT NOT NULL,"
    "  progress  INT NOT NULL DEFAULT 0,"
    "  claimed   INT NOT NULL DEFAULT 0,"
    "  PRIMARY KEY (user_id, day, quest_idx))",
    # One-open invariants (one dungeon lobby per server, one live challenge
    # per challenger, one live offer per seller) ride this table's PRIMARY
    # KEY: an INSERT ... ON CONFLICT DO NOTHING is an atomic insert-once
    # claim, judged by rowcount — the allowlist-legal replacement for the
    # partial unique indexes the host refuses.
    "CREATE TABLE IF NOT EXISTS locks ("
    "  name       TEXT PRIMARY KEY,"
    "  ref        TEXT NOT NULL,"
    "  created_at BIGINT NOT NULL DEFAULT 0)",
    "CREATE TABLE IF NOT EXISTS dungeons ("
    "  id           TEXT PRIMARY KEY,"
    "  dungeon_key  TEXT NOT NULL,"
    "  channel_id   TEXT NOT NULL DEFAULT '',"
    "  leader_id    TEXT NOT NULL,"
    "  status       TEXT NOT NULL DEFAULT 'open',"
    "  created_at   BIGINT NOT NULL,"
    "  member_count INT NOT NULL DEFAULT 0)",
    # NOTE: the host only allows these statement types: ALTER TABLE,
    # CREATE INDEX, CREATE TABLE, DELETE, DROP INDEX, DROP TABLE, INSERT,
    # SELECT, UPDATE. CREATE UNIQUE INDEX is NOT among them (verified live),
    # so one-open invariants live in the `locks` table below instead.
    "CREATE TABLE IF NOT EXISTS dungeon_members ("
    "  dungeon_id TEXT NOT NULL,"
    "  user_id    TEXT NOT NULL,"
    "  username   TEXT NOT NULL DEFAULT '',"
    "  joined_at  BIGINT NOT NULL DEFAULT 0,"
    "  PRIMARY KEY (dungeon_id, user_id))",
    "CREATE TABLE IF NOT EXISTS duels ("
    "  id              TEXT PRIMARY KEY,"
    "  challenger_id   TEXT NOT NULL,"
    "  target_id       TEXT NOT NULL,"
    "  challenger_name TEXT NOT NULL DEFAULT '',"
    "  target_name     TEXT NOT NULL DEFAULT '',"
    "  bet             BIGINT NOT NULL DEFAULT 0,"
    "  status          TEXT NOT NULL DEFAULT 'open',"
    "  channel_id      TEXT NOT NULL DEFAULT '',"
    "  created_at      BIGINT NOT NULL,"
    "  epoch           INT NOT NULL DEFAULT 0)",
    "CREATE TABLE IF NOT EXISTS trades ("
    "  id          TEXT PRIMARY KEY,"
    "  seller_id   TEXT NOT NULL,"
    "  buyer_id    TEXT NOT NULL,"
    "  seller_name TEXT NOT NULL DEFAULT '',"
    "  item_id     TEXT NOT NULL,"
    "  qty         INT NOT NULL DEFAULT 1,"
    "  price       BIGINT NOT NULL DEFAULT 0,"
    "  status      TEXT NOT NULL DEFAULT 'open',"
    "  channel_id  TEXT NOT NULL DEFAULT '',"
    "  created_at  BIGINT NOT NULL,"
    "  epoch       INT NOT NULL DEFAULT 0)",
    "CREATE TABLE IF NOT EXISTS guilds ("
    "  key          TEXT PRIMARY KEY,"
    "  name         TEXT NOT NULL,"
    "  leader_id    TEXT NOT NULL,"
    "  member_count INT NOT NULL DEFAULT 0,"
    "  created_at   BIGINT NOT NULL)",
    # user_id is the PRIMARY KEY: one guild per player, enforced atomically.
    "CREATE TABLE IF NOT EXISTS guild_members ("
    "  user_id   TEXT PRIMARY KEY,"
    "  guild_key TEXT NOT NULL,"
    "  username  TEXT NOT NULL DEFAULT '',"
    "  joined_at BIGINT NOT NULL DEFAULT 0)",
    "CREATE TABLE IF NOT EXISTS arena_entries ("
    "  day        TEXT NOT NULL,"
    "  user_id    TEXT NOT NULL,"
    "  username   TEXT NOT NULL DEFAULT '',"
    "  score      INT NOT NULL DEFAULT 0,"
    "  paid_fee   BIGINT NOT NULL DEFAULT 0,"
    "  channel_id TEXT NOT NULL DEFAULT '',"
    "  created_at BIGINT NOT NULL DEFAULT 0,"
    "  PRIMARY KEY (day, user_id))",
    # A day's row here means its tournament was paid out (insert-once claim).
    "CREATE TABLE IF NOT EXISTS arena_days ("
    "  day         TEXT PRIMARY KEY,"
    "  resolved_at BIGINT NOT NULL DEFAULT 0)",
)

# Existence probes, paired 1:1 (in order) with _SCHEMA: a successful SELECT
# proves the table exists, so its CREATE is never sent. SELECTs are free; the
# host's hourly DDL budget is spent ONLY on statements that change something.
_SCHEMA_PROBES = (
    "SELECT user_id FROM players LIMIT 1",
    "SELECT user_id FROM inventory LIMIT 1",
    "SELECT user_id FROM quest_progress LIMIT 1",
    "SELECT name FROM locks LIMIT 1",
    "SELECT id FROM dungeons LIMIT 1",
    "SELECT user_id FROM dungeon_members LIMIT 1",
    "SELECT id FROM duels LIMIT 1",
    "SELECT id FROM trades LIMIT 1",
    "SELECT key FROM guilds LIMIT 1",
    "SELECT user_id FROM guild_members LIMIT 1",
    "SELECT user_id FROM arena_entries LIMIT 1",
    "SELECT day FROM arena_days LIMIT 1",
)

# Upgrades for servers that installed the Phase-1 schema. New installs get the
# columns from CREATE TABLE; these no-op there (IF NOT EXISTS).
_MIGRATIONS = (
    "ALTER TABLE players ADD COLUMN IF NOT EXISTS last_dungeon_at BIGINT NOT NULL DEFAULT 0",
    "ALTER TABLE players ADD COLUMN IF NOT EXISTS sword_enchant INT NOT NULL DEFAULT 0",
    "ALTER TABLE players ADD COLUMN IF NOT EXISTS armor_enchant INT NOT NULL DEFAULT 0",
    "ALTER TABLE players ADD COLUMN IF NOT EXISTS last_duel_at BIGINT NOT NULL DEFAULT 0",
    "ALTER TABLE players ADD COLUMN IF NOT EXISTS pet TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE players ADD COLUMN IF NOT EXISTS rekindles INT NOT NULL DEFAULT 0",
    "ALTER TABLE duels ADD COLUMN IF NOT EXISTS epoch INT NOT NULL DEFAULT 0",
    "ALTER TABLE trades ADD COLUMN IF NOT EXISTS epoch INT NOT NULL DEFAULT 0",
)

# Probes paired 1:1 (in order) with _MIGRATIONS: selecting the column proves
# the ALTER is a no-op. Critical, not just an optimization — the host counts
# no-op ALTERs against the DDL budget, so re-issuing all eight would both burn
# the full hourly budget on every boot AND make a zero-failure bootstrap pass
# (the condition for the KV revision marker) impossible.
_MIGRATION_PROBES = (
    "SELECT last_dungeon_at FROM players LIMIT 1",
    "SELECT sword_enchant FROM players LIMIT 1",
    "SELECT armor_enchant FROM players LIMIT 1",
    "SELECT last_duel_at FROM players LIMIT 1",
    "SELECT pet FROM players LIMIT 1",
    "SELECT rekindles FROM players LIMIT 1",
    "SELECT epoch FROM duels LIMIT 1",
    "SELECT epoch FROM trades LIMIT 1",
)


_SCHEMA_REV = 2          # bump whenever _SCHEMA or _MIGRATIONS change
_SCHEMA_RETRY_SECONDS = 660  # the host's DDL budget refills hourly; poll a
                             # little faster so we catch the refill promptly
_schema_ok: set = set()      # server ids whose schema rev is confirmed
_schema_retry: dict = {}     # server id -> earliest next bootstrap attempt


def _ensure_schema(ctx, server_id=""):
    """Rate-limit-aware bootstrap.

    The live host caps schema changes ("DDL rate limit: max 5 DDL statements
    per hour"), so a fresh install cannot land all its tables in one go. Every
    statement is individually walled (one rejection must never block the
    tables after it), a KV revision marker makes fully-provisioned servers
    boot with ZERO DDL, and incomplete bootstraps are retried periodically
    from _begin until every statement has landed.
    """
    if server_id and server_id in _schema_ok:
        return True
    try:
        if int(ctx.kv.get(KV_SCHEMA_REV) or 0) >= _SCHEMA_REV:
            if server_id:
                _schema_ok.add(server_id)
            return True
    except (SdkError, RuntimeError, TypeError, ValueError):
        pass  # KV hiccup or junk value: fall through to the walled DDL
    failures = 0
    # strict zip: a probe/DDL length drift must fail loudly, never silently
    # truncate trailing statements (which could set the marker incompletely).
    for probe, ddl in zip(_SCHEMA_PROBES + _MIGRATION_PROBES,
                          _SCHEMA + _MIGRATIONS, strict=True):
        try:
            ctx.sql.query_one(probe)
            continue  # already provisioned — don't spend DDL budget on a no-op
        except (SdkError, RuntimeError):
            pass  # missing relation/column (or a transient): issue the DDL
        try:
            ctx.sql.execute(ddl)
        except (SdkError, RuntimeError) as exc:
            failures += 1
            ctx.log("EmberQuest schema statement skipped: " + str(exc),
                    level="warning")
    if failures:
        ctx.log("EmberQuest schema incomplete: " + str(failures) + " statements "
                "deferred by the host's hourly DDL budget; retrying periodically. "
                "Features touching missing tables will say the Cinderwilds are "
                "still settling in.", level="warning")
        return False
    _quietly(ctx.kv.set, KV_SCHEMA_REV, _SCHEMA_REV)
    if server_id:
        _schema_ok.add(server_id)
    return True


def _maybe_retry_schema(ctx, event):
    """Cheap per-command check: while a server's schema is incomplete, retry
    the bootstrap every ~11 minutes so the hourly DDL budget refill is picked
    up without waiting for a process restart."""
    sid = str(event.get("guild_id") or "")
    if sid in _schema_ok:
        return
    now = _now()
    if now < _schema_retry.get(sid, 0):
        return
    _schema_retry[sid] = now + _SCHEMA_RETRY_SECONDS
    _ensure_schema(ctx, sid)


@plugin.on_install
def on_install(ctx):
    _ensure_schema(ctx)
    if not ctx.kv.exists(KV_MULTIPLIER):
        ctx.kv.set(KV_MULTIPLIER, 1.0)
    if not ctx.kv.exists(KV_CHANNEL):
        ctx.kv.set(KV_CHANNEL, "")
    ctx.log("EmberQuest installed: schema ready, default settings seeded")


@plugin.on_ready
def on_ready(ctx):
    # Idempotent safety net: in pool mode this fires once per (worker, server)
    # before the first event, and lifecycle events that arrive pre-boot are
    # silently dropped by the SDK — so install alone can't be relied on.
    _ensure_schema(ctx)


# --- SQL statements (whole literals only) -------------------------------------

# Atomic cooldown claim: succeeds (rowcount 1) for exactly one concurrent event.
_CLAIM_COOLDOWN = {
    "last_hunt_at": ("UPDATE players SET last_hunt_at = %s "
                     "WHERE user_id = %s AND last_hunt_at <= %s"),
    "last_adventure_at": ("UPDATE players SET last_adventure_at = %s "
                          "WHERE user_id = %s AND last_adventure_at <= %s"),
    "last_daily_at": ("UPDATE players SET last_daily_at = %s "
                      "WHERE user_id = %s AND last_daily_at <= %s"),
    "last_dungeon_at": ("UPDATE players SET last_dungeon_at = %s "
                        "WHERE user_id = %s AND last_dungeon_at <= %s"),
    "last_duel_at": ("UPDATE players SET last_duel_at = %s "
                     "WHERE user_id = %s AND last_duel_at <= %s"),
}

# Encounter settlement: xp/coins relative; level/max_hp ratchet up only
# (a slower concurrent writer must never downgrade them); coins clamp at 0.
# The trailing `rekindles = %s` is the EPOCH GUARD: a Rekindling is the one
# legitimate downgrade, so any settle computed from a pre-burn read must land
# in the void rather than resurrect the old character.
_ENCOUNTER_UPDATE = (
    "UPDATE players SET username = %s, xp = xp + %s, "
    "level = CASE WHEN level < %s THEN %s ELSE level END, "
    "max_hp = CASE WHEN max_hp < %s THEN %s ELSE max_hp END, "
    "hp = %s, "
    "coins = CASE WHEN coins + %s < 0 THEN 0 ELSE coins + %s END, "
    "last_action_at = %s WHERE user_id = %s AND rekindles = %s"
)

_HEAL_WITH_POTION = ("UPDATE players SET username = %s, hp = %s, last_action_at = %s "
                     "WHERE user_id = %s")
_HEAL_WITH_PURCHASE = ("UPDATE players SET username = %s, hp = %s, coins = coins - %s, "
                       "last_action_at = %s WHERE user_id = %s AND coins >= %s")

# Changing a gear slot always clears its enchantment — enchants are bound to
# the specific equipped item. The trailing CAS (… AND sword = %s) voids the
# equip if the slot changed between the stat comparison and this statement.
_EQUIP_CAS = {
    "sword": ("UPDATE players SET username = %s, sword = %s, sword_enchant = 0 "
              "WHERE user_id = %s AND sword = %s"),
    "armor": ("UPDATE players SET username = %s, armor = %s, armor_enchant = 0 "
              "WHERE user_id = %s AND armor = %s"),
}

# /equip's variant additionally proves ownership IN the same statement — a
# concurrent sell/trade escrow can't slip between the check and the swap.
_EQUIP_OWNED_CAS = {
    "sword": ("UPDATE players SET username = %s, sword = %s, sword_enchant = 0 "
              "WHERE user_id = %s AND sword = %s AND EXISTS "
              "(SELECT 1 FROM inventory WHERE user_id = %s AND item_id = %s AND qty > 0)"),
    "armor": ("UPDATE players SET username = %s, armor = %s, armor_enchant = 0 "
              "WHERE user_id = %s AND armor = %s AND EXISTS "
              "(SELECT 1 FROM inventory WHERE user_id = %s AND item_id = %s AND qty > 0)"),
}

# Same idea for the leash: setting an active pet proves ownership atomically.
_SET_PET_OWNED = ("UPDATE players SET pet = %s WHERE user_id = %s AND EXISTS "
                  "(SELECT 1 FROM inventory WHERE user_id = %s AND item_id = %s "
                  "AND qty > 0)")

_CREDIT_COINS = "UPDATE players SET username = %s, coins = coins + %s WHERE user_id = %s"

# Enchant upgrade is a compare-and-swap on both the level and the gear itself:
# if either changed mid-ritual, the attempt is void and the costs are refunded.
_ENCHANT_SET = {
    "sword": ("UPDATE players SET sword_enchant = %s "
              "WHERE user_id = %s AND sword_enchant = %s AND sword = %s"),
    "armor": ("UPDATE players SET armor_enchant = %s "
              "WHERE user_id = %s AND armor_enchant = %s AND armor = %s"),
}

# --- Dungeon statements --------------------------------------------------------

_EXPIRE_ONE_LOBBY = ("UPDATE dungeons SET status = 'expired' "
                     "WHERE id = %s AND status = 'open'")
_EXPIRE_RESOLVING_LOBBY = ("UPDATE dungeons SET status = 'expired' "
                           "WHERE id = %s AND status = 'resolving'")
# Terminal expeditions hold no escrow; prune rosters first, then the rows.
_PRUNE_DUNGEON_MEMBERS = ("DELETE FROM dungeon_members WHERE dungeon_id IN "
                          "(SELECT id FROM dungeons WHERE status NOT IN "
                          "('open', 'resolving') AND created_at < %s)")
_PRUNE_DUNGEONS = ("DELETE FROM dungeons WHERE status NOT IN "
                   "('open', 'resolving') AND created_at < %s")

# Sequential opens converge via WHERE NOT EXISTS; truly concurrent opens are
# serialized by the 'dungeon' lock claim (the host's allowlist forbids the
# unique index that once backstopped this).
_OPEN_DUNGEON = (
    "INSERT INTO dungeons (id, dungeon_key, channel_id, leader_id, status, created_at, "
    "member_count) "
    "SELECT %s, %s, %s, %s, 'open', %s, 1 "
    "WHERE NOT EXISTS (SELECT 1 FROM dungeons WHERE status = 'open')"
)

# Party seats are claimed with a guarded relative increment — atomic under
# concurrent joins, where an INSERT…SELECT COUNT(*) check is not. The
# membership row lands after the seat is held; a failed insert releases it.
_CLAIM_SEAT = ("UPDATE dungeons SET member_count = member_count + 1 "
               "WHERE id = %s AND status = 'open' AND member_count < %s")
_RELEASE_SEAT = ("UPDATE dungeons SET member_count = member_count - 1 "
                 "WHERE id = %s AND member_count > 0")
_MEMBER_INSERT = ("INSERT INTO dungeon_members (dungeon_id, user_id, username, joined_at) "
                  "VALUES (%s, %s, %s, %s) ON CONFLICT (dungeon_id, user_id) DO NOTHING")

# The 2h player cooldown is claimed AT JOIN (atomic, prevents double-dipping
# across overlapping lobbies) and refunded if the lobby expires unfought.
_REFUND_DUNGEON_COOLDOWN = "UPDATE players SET last_dungeon_at = 0 WHERE user_id = %s"

# Exactly one event wins the right to resolve the expedition.
_CLAIM_RESOLUTION = ("UPDATE dungeons SET status = 'resolving' "
                     "WHERE id = %s AND status = 'open'")
_FINISH_DUNGEON = "UPDATE dungeons SET status = 'resolved' WHERE id = %s"

# --- Lock statements (one-open invariants, host-allowlist legal) -----------------

_LOCK_CLAIM = ("INSERT INTO locks (name, ref, created_at) VALUES (%s, %s, %s) "
               "ON CONFLICT (name) DO NOTHING")
_LOCK_READ = "SELECT ref, created_at FROM locks WHERE name = %s"
# Release is CAS'd on the ref so a later holder is never evicted by accident.
_LOCK_RELEASE = "DELETE FROM locks WHERE name = %s AND ref = %s"

# Status lookups for self-healing stale locks (one literal per domain — SQL is
# never assembled from variables).
_LOCK_HOLDER_STATUS = {
    "dungeon": "SELECT status FROM dungeons WHERE id = %s",
    "duel": "SELECT status FROM duels WHERE id = %s",
    "trade": "SELECT status FROM trades WHERE id = %s",
}
# Ambiguous-failure re-reads check OWNERSHIP too: an id collision must never
# report someone else's live row as your own successful open.
_DUEL_OWNER = "SELECT challenger_id FROM duels WHERE id = %s"
_TRADE_OWNER = "SELECT seller_id FROM trades WHERE id = %s"
_DUNGEON_OWNER = "SELECT leader_id FROM dungeons WHERE id = %s"
# A lock is stale once its holder is terminal/missing, or after the domain's
# TTL plus a grace period (covers a handler that died before releasing).
_LOCK_MAX_AGE = {
    "dungeon": game.DUNGEON_LOBBY_TTL + 600,
    "duel": game.DUEL_TTL + 600,
    "trade": game.TRADE_TTL + 600,
}


_LOCK_BIRTH_GRACE = 60  # every open claims the lock one roundtrip BEFORE
                        # inserting its row — a young lock with no holder row
                        # is mid-birth, NOT stale (breaking it would let two
                        # concurrent opens both succeed)


def _acquire_lock(ctx, kind, name, ref, now) -> bool:
    """Atomic insert-once claim with self-healing: a lock whose holder is
    finished (or long overdue) is broken and re-claimed in one pass."""
    for _ in range(2):
        if ctx.sql.execute(_LOCK_CLAIM, [name, ref, now]):
            return True
        lock = ctx.sql.query_one(_LOCK_READ, [name])
        if lock is None:
            continue  # released between our claim and read — try again
        holder = ctx.sql.query_one(_LOCK_HOLDER_STATUS[kind], [lock["ref"]])
        age = now - int(lock["created_at"])
        if holder is None:
            stale = age > _LOCK_BIRTH_GRACE  # row mid-insert vs orphaned claim
        else:
            stale = (holder["status"] not in ("open", "resolving")
                     or age > _LOCK_MAX_AGE[kind])
        if not stale:
            return False  # genuinely held
        ctx.sql.execute(_LOCK_RELEASE, [name, lock["ref"]])  # stale: break it
    return ctx.sql.execute(_LOCK_CLAIM, [name, ref, now]) > 0


def _release_lock(ctx, name, ref):
    _quietly(ctx.sql.execute, _LOCK_RELEASE, [name, ref])


# --- Duel statements (Phase 3) ---------------------------------------------------

# The duels_one_live_per_challenger index rejects a second concurrent open;
# the epoch WHERE refuses a challenge whose escrow was debited from a
# character that Rekindled mid-handler (the stake burns via the epoch refund).
_OPEN_DUEL = ("INSERT INTO duels (id, challenger_id, target_id, challenger_name, "
              "target_name, bet, status, channel_id, created_at, epoch) "
              "SELECT %s, %s, %s, %s, %s, %s, 'open', %s, %s, %s "
              "WHERE (SELECT rekindles FROM players WHERE user_id = %s) = %s")
# Status transitions are all CAS — every escrow refund happens exactly once.
_DUEL_ACCEPT_CAS = "UPDATE duels SET status = 'resolving' WHERE id = %s AND status = 'open'"
_DUEL_REOPEN = "UPDATE duels SET status = 'open' WHERE id = %s AND status = 'resolving'"
_DUEL_DECLINE_CAS = "UPDATE duels SET status = 'declined' WHERE id = %s AND status = 'open'"
_DUEL_EXPIRE_CAS = "UPDATE duels SET status = 'expired' WHERE id = %s AND status = 'open'"
# Terminal exit from a failed settlement — the refund rides on this rowcount.
_DUEL_VOID_RESOLVING = "UPDATE duels SET status = 'expired' WHERE id = %s AND status = 'resolving'"
_DUEL_FINISH = "UPDATE duels SET status = 'resolved' WHERE id = %s"
_REFUND_DUEL_COOLDOWN = "UPDATE players SET last_duel_at = 0 WHERE user_id = %s"
_STUCK_RESOLVING_DUELS = ("SELECT * FROM duels WHERE status = 'resolving' "
                          "AND created_at < %s")
_STUCK_RESOLVING_TRADES = ("SELECT * FROM trades WHERE status = 'resolving' "
                           "AND created_at < %s")
_PRUNE_LOCKS = "DELETE FROM locks WHERE created_at < %s"
# Terminal rows have no escrow; prune them after a week so tables stay bounded.
_PRUNE_DUELS = ("DELETE FROM duels WHERE status NOT IN ('open', 'resolving') "
                "AND created_at < %s")

# Duel/arena settlements ratchet level and max_hp but never touch HP — honor
# bouts leave no wounds (and so no level-up heal either). Epoch-guarded like
# every settle: a Rekindled character never inherits stale spoils.
_DUEL_SETTLE = ("UPDATE players SET xp = xp + %s, "
                "level = CASE WHEN level < %s THEN %s ELSE level END, "
                "max_hp = CASE WHEN max_hp < %s THEN %s ELSE max_hp END, "
                "coins = coins + %s, last_duel_at = %s "
                "WHERE user_id = %s AND rekindles = %s")
_AWARD_XP = ("UPDATE players SET xp = xp + %s, "
             "level = CASE WHEN level < %s THEN %s ELSE level END, "
             "max_hp = CASE WHEN max_hp < %s THEN %s ELSE max_hp END "
             "WHERE user_id = %s AND rekindles = %s")

# Epoch-guarded credit/grant for any flow whose debit-or-read happened earlier
# in the same handler: if a Rekindling landed in between, the value burns.
_CREDIT_COINS_EPOCH = ("UPDATE players SET username = %s, coins = coins + %s "
                       "WHERE user_id = %s AND rekindles = %s")
_ADD_ITEM_EPOCH = (
    "INSERT INTO inventory (user_id, item_id, qty) "
    "SELECT %s, %s, %s "
    "WHERE (SELECT rekindles FROM players WHERE user_id = %s) = %s "
    "ON CONFLICT (user_id, item_id) DO UPDATE SET qty = inventory.qty + %s"
)

# --- Trade statements (Phase 3) ----------------------------------------------------

_OPEN_TRADE = ("INSERT INTO trades (id, seller_id, buyer_id, seller_name, item_id, qty, "
               "price, status, channel_id, created_at, epoch) "
               "SELECT %s, %s, %s, %s, %s, %s, %s, 'open', %s, %s, %s "
               "WHERE (SELECT rekindles FROM players WHERE user_id = %s) = %s")
_TRADE_ACCEPT_CAS = "UPDATE trades SET status = 'resolving' WHERE id = %s AND status = 'open'"
_TRADE_REOPEN = "UPDATE trades SET status = 'open' WHERE id = %s AND status = 'resolving'"
_TRADE_DECLINE_CAS = "UPDATE trades SET status = 'declined' WHERE id = %s AND status = 'open'"
_TRADE_CANCEL_CAS = "UPDATE trades SET status = 'cancelled' WHERE id = %s AND status = 'open'"
_TRADE_EXPIRE_CAS = "UPDATE trades SET status = 'expired' WHERE id = %s AND status = 'open'"
_TRADE_VOID_RESOLVING = "UPDATE trades SET status = 'expired' WHERE id = %s AND status = 'resolving'"
_TRADE_FINISH = "UPDATE trades SET status = 'resolved' WHERE id = %s"
_PRUNE_TRADES = ("DELETE FROM trades WHERE status NOT IN ('open', 'resolving') "
                 "AND created_at < %s")

# Bulk guarded take for escrow (qty can be > 1, single atomic statement).
_INVENTORY_TAKE_N = ("UPDATE inventory SET qty = qty - %s "
                     "WHERE user_id = %s AND item_id = %s AND qty >= %s")

# --- Guild statements (Phase 3) -------------------------------------------------------

_GUILD_INSERT = ("INSERT INTO guilds (key, name, leader_id, member_count, created_at) "
                 "VALUES (%s, %s, %s, 1, %s) ON CONFLICT (key) DO NOTHING")
_GUILD_MEMBER_INSERT = ("INSERT INTO guild_members (user_id, guild_key, username, joined_at) "
                        "VALUES (%s, %s, %s, %s) ON CONFLICT (user_id) DO NOTHING")
_GUILD_SEAT_CLAIM = ("UPDATE guilds SET member_count = member_count + 1 "
                     "WHERE key = %s AND member_count < %s")
_GUILD_SEAT_RELEASE = ("UPDATE guilds SET member_count = member_count - 1 "
                       "WHERE key = %s AND member_count > 0")
_GUILD_MEMBER_DELETE = "DELETE FROM guild_members WHERE user_id = %s AND guild_key = %s"
_GUILD_TRANSFER = "UPDATE guilds SET leader_id = %s WHERE key = %s AND leader_id = %s"
_GUILD_DELETE_EMPTY = "DELETE FROM guilds WHERE key = %s AND member_count <= 0"

# --- Arena statements (Phase 3) -----------------------------------------------------------

# Entry is gated on the day not being paid out yet (UTC-midnight race: an
# in-flight entry must not buy into a tournament that already settled).
_ARENA_ENTER = ("INSERT INTO arena_entries (day, user_id, username, score, paid_fee, "
                "channel_id, created_at) "
                "SELECT %s, %s, %s, %s, %s, %s, %s "
                "WHERE NOT EXISTS (SELECT 1 FROM arena_days WHERE day = %s) "
                "ON CONFLICT (day, user_id) DO NOTHING")
# Insert-once claim: exactly one event pays out a finished day.
_ARENA_CLAIM_DAY = ("INSERT INTO arena_days (day, resolved_at) VALUES (%s, %s) "
                    "ON CONFLICT (day) DO NOTHING")
_PRUNE_ARENA_ENTRIES = "DELETE FROM arena_entries WHERE day < %s"
_PRUNE_ARENA_DAYS = "DELETE FROM arena_days WHERE day < %s"

# --- Quest statements (Phase 4) -----------------------------------------------------------

_QUEST_PROGRESS_UPSERT = (
    "INSERT INTO quest_progress (user_id, day, quest_idx, progress, claimed) "
    "VALUES (%s, %s, %s, %s, 0) "
    "ON CONFLICT (user_id, day, quest_idx) DO UPDATE "
    "SET progress = quest_progress.progress + %s"
)
# The claim rides on this rowcount: complete, unclaimed, exactly once.
_QUEST_CLAIM_CAS = (
    "UPDATE quest_progress SET claimed = 1 "
    "WHERE user_id = %s AND day = %s AND quest_idx = %s "
    "AND claimed = 0 AND progress >= %s"
)
_PRUNE_QUESTS = "DELETE FROM quest_progress WHERE day < %s"

# --- Pet & Rekindling statements (Phase 4) ----------------------------------------------------

# CAS on the current companion — only clears if it's still the one leaving.
_CLEAR_PET_CAS = "UPDATE players SET pet = '' WHERE user_id = %s AND pet = %s"
# A pet fresh from a cache pads an empty leash automatically — but only if
# it is genuinely owned at that instant (proven in-statement).
_ADOPT_IF_PETLESS = ("UPDATE players SET pet = %s WHERE user_id = %s AND pet = '' "
                     "AND EXISTS (SELECT 1 FROM inventory WHERE user_id = %s "
                     "AND item_id = %s AND qty > 0)")
# Duplicate conversion is a CAS on qty > 1: of two concurrent hatches of the
# same companion, exactly one converts — the pet itself is never destroyed.
_TAKE_DUPLICATE_PET = ("UPDATE inventory SET qty = qty - 1 "
                       "WHERE user_id = %s AND item_id = %s AND qty > 1")

# The whole prestige reset is ONE guarded statement: the level check and the
# rekindle increment land together, so a stale or double-pressed confirm
# button can never fire twice. last_daily_at survives (no bonus daily).
_REKINDLE_RESET = (
    "UPDATE players SET level = 1, xp = 0, hp = 100, max_hp = 100, coins = 0, "
    "sword = 'fists', armor = 'cloth', sword_enchant = 0, armor_enchant = 0, "
    "last_hunt_at = 0, last_adventure_at = 0, last_dungeon_at = 0, "
    "last_duel_at = 0, rekindles = rekindles + 1, last_action_at = %s "
    "WHERE user_id = %s AND level >= %s"
)
_WIPE_INVENTORY_EXCEPT = "DELETE FROM inventory WHERE user_id = %s AND item_id != %s"
# The spared companion survives as ONE copy — a trade-stacked pile of the
# active pet must not carry its sell value across the burn.
_CLAMP_KEPT_PET = ("UPDATE inventory SET qty = 1 "
                   "WHERE user_id = %s AND item_id = %s AND qty > 1")

_DUNGEON_MEMBER_UPDATE = (
    "UPDATE players SET xp = xp + %s, "
    "level = CASE WHEN level < %s THEN %s ELSE level END, "
    "max_hp = CASE WHEN max_hp < %s THEN %s ELSE max_hp END, "
    "hp = %s, coins = coins + %s, "
    "last_dungeon_at = %s, last_action_at = %s "
    "WHERE user_id = %s AND rekindles = %s"
)

_COINS_SUB_GUARDED = ("UPDATE players SET username = %s, coins = coins - %s "
                      "WHERE user_id = %s AND coins >= %s")
# Escrow debits are epoch-stamped so a debit and its eventual refund share one
# epoch by construction — a handler straddling a Rekindling can neither consume
# post-burn assets nor strand its refund.
_COINS_SUB_GUARDED_EPOCH = ("UPDATE players SET username = %s, coins = coins - %s "
                            "WHERE user_id = %s AND coins >= %s AND rekindles = %s")
_INVENTORY_TAKE_N_EPOCH = ("UPDATE inventory SET qty = qty - %s "
                           "WHERE user_id = %s AND item_id = %s AND qty >= %s "
                           "AND (SELECT rekindles FROM players WHERE user_id = %s) = %s")

# Guarded take: rowcount 0 means the item wasn't there — qty can never go negative.
_INVENTORY_TAKE = ("UPDATE inventory SET qty = qty - 1 "
                   "WHERE user_id = %s AND item_id = %s AND qty > 0")


# --- Shared helpers ----------------------------------------------------------

def _options(event) -> dict:
    # The live gateway sends both `options` and the deprecated alias
    # `command_options` today; accept either (and either shape) so a gateway
    # field drift can't silently strip every command's arguments — the same
    # class of breakage user_name/user_username caused for usernames.
    raw = event.get("options") or event.get("command_options") or []
    if isinstance(raw, dict):
        return dict(raw)
    return {o.get("name"): o.get("value") for o in raw}


def _username(event) -> str:
    # The SDK's typed schema documents `user_name`, but the LIVE gateway sends
    # `user_username` (verified in production logs) — accept both.
    return str(event.get("user_name") or event.get("user_username") or "")


def _embed(title, description="", fields=None, footer=None) -> dict:
    embed = {"title": title, "color": EMBED_COLOR}
    if description:
        embed["description"] = description
    if fields:
        embed["fields"] = fields
    if footer:
        embed["footer"] = {"text": footer}
    return embed


def _quietly(fn, *args, **kwargs):
    """Best-effort compensation step: one failed refund must never block the
    remaining ones (or re-raise out of an except wall)."""
    try:
        fn(*args, **kwargs)
    except (SdkError, RuntimeError):
        pass


def _try_respond(ctx, message, ephemeral=True):
    try:
        ctx.interaction.respond(content=message, ephemeral=ephemeral)
        return
    except (SdkError, RuntimeError):
        pass
    try:
        # respond() fails if the interaction was already deferred — deliver the
        # message via followup so the user never stares at "thinking…" forever.
        ctx.interaction.followup(content=message, ephemeral=ephemeral)
    except (SdkError, RuntimeError):
        pass  # already responded, or the host refused — nothing useful left to do


def _safe(fn):
    """Last-resort error wall: log and answer rather than eat the interaction."""
    @functools.wraps(fn)
    def wrapper(ctx, event):
        try:
            fn(ctx, event)
        except RateLimitError as exc:
            wait = max(int(getattr(exc, "retry_after", 0) or 0), 1)
            _try_respond(ctx, f"⏳ The forge is overloaded — try again in {wait}s.")
        except (SdkError, RuntimeError) as exc:
            ctx.log("EmberQuest handler error: " + str(exc), level="error",
                    request_id=ctx.request_id)
            missing_relation = ("does not exist" in str(exc)        # postgres
                                or "no such table" in str(exc))     # sqlite (tests)
            if missing_relation:
                # A table hasn't landed yet: fresh installs provision over a
                # few hourly windows (the host rate-limits schema changes).
                _try_respond(ctx, "🔥 The Cinderwilds are still settling in — a fresh "
                                  "install raises its grounds over the first few hours. "
                                  "This feature will light up shortly; try again soon.")
            else:
                _try_respond(ctx, "🔥 Something went wrong in the Cinderwilds — please try again.")
    return wrapper


def _settings(ctx) -> tuple[float, str]:
    try:
        multiplier = float(ctx.kv.get(KV_MULTIPLIER) or 1.0)
    except (TypeError, ValueError):
        multiplier = 1.0
    multiplier = min(10.0, max(0.1, multiplier))
    channel = str(ctx.kv.get(KV_CHANNEL) or "")
    return multiplier, channel


def _get_player(ctx, user_id):
    return ctx.sql.query_one("SELECT * FROM players WHERE user_id = %s", [str(user_id)])


def _apply_regen(player: dict, now: int) -> dict:
    """Lazy regen with a banked anchor.

    The anchor (`last_action_at`) only advances by the regen time actually
    consumed, so partial progress toward the next HP tick survives frequent
    commands (otherwise anyone hunting every 60s would never regen at all).
    """
    player = dict(player)
    healed_to = game.regen_hp(player["hp"], player["max_hp"], player["last_action_at"], now)
    if healed_to >= player["max_hp"] or player["last_action_at"] <= 0:
        anchor = now  # at full HP (or never acted): no pending progress to bank
    else:
        anchor = player["last_action_at"] + (healed_to - player["hp"]) * game.REGEN_SECONDS_PER_HP
    player["hp"] = healed_to
    player["_regen_anchor"] = anchor
    return player


def _boosts_for(player, now) -> dict:
    """Passive multipliers from companion + Rekindlings + active season."""
    return game.player_boosts(player.get("pet") or "",
                              int(player.get("rekindles") or 0),
                              game.current_season(now))


def _quest_event(ctx, user_id, event_key, now, amount=1):
    """Tick today's quest counters for an action. Called via _quietly at every
    hook site — quest bookkeeping must never break the action it rode on."""
    day = game.arena_day(now)
    for idx, quest in enumerate(game.daily_quests(str(user_id), day)):
        if quest["key"] == event_key:
            ctx.sql.execute(_QUEST_PROGRESS_UPSERT,
                            [str(user_id), day, idx, amount, amount])


def _begin(ctx, event, command, *, needs_player=True):
    """Common command preamble: metrics, channel gate, player lookup.

    Returns a state dict, or None if the command was already answered.
    """
    ctx.metrics.record("commands", tags={"command": command})
    _maybe_retry_schema(ctx, event)
    multiplier, channel = _settings(ctx)
    if channel and str(event.get("channel_id") or "") != channel:
        _try_respond(ctx, f"🔥 EmberQuest lives in <#{channel}> on this server.")
        return None
    player = None
    if needs_player:
        player = _get_player(ctx, event.get("user_id"))
        if player is None:
            _try_respond(ctx, "You haven't begun your tale — run **/start** first!")
            return None
    return {"multiplier": multiplier, "player": player}


_COOLDOWN_MESSAGES = {
    "hunt": "⏳ Catch your breath — **/hunt** is ready in {duration}.",
    "adventure": "⏳ The wilds need time to stir — **/adventure** is ready in {duration}.",
    "daily": "🌅 Your stipend renews in **{duration}**.",
}


def _claim_cooldown(ctx, player, kind, column, cooldown, now) -> bool:
    """Atomically claim a cooldown window; False means already on cooldown.

    The conditional UPDATE is the real gate (exactly one concurrent event can
    win it); the ephemeral check is just a cheap spam shield in front.
    """
    user_id = player["user_id"]
    guard = ctx.ephemeral.cooldown_check(f"{kind}:{user_id}")
    if guard.get("active"):
        remaining = int(guard.get("remaining_seconds") or 0) + 1
        _try_respond(ctx, _COOLDOWN_MESSAGES[kind].format(duration=game.fmt_duration(remaining)))
        return False
    claimed = ctx.sql.execute(_CLAIM_COOLDOWN[column], [now, user_id, now - cooldown])
    if not claimed:
        remaining = max(1, cooldown - (now - int(player[column])))
        _try_respond(ctx, _COOLDOWN_MESSAGES[kind].format(duration=game.fmt_duration(remaining)))
        return False
    ctx.ephemeral.cooldown_set(f"{kind}:{user_id}", ttl_seconds=cooldown)
    return True


def _hp_field(hp, max_hp):
    return {"name": "HP", "value": f"`{game.progress_bar(hp, max_hp)}` {hp}/{max_hp}", "inline": True}


def _combat_buttons(user_id, hp, max_hp):
    buttons = [Button("Hunt again", f"{HUNT_AGAIN_PREFIX}{user_id}", style="primary", emoji="🗡️")]
    if hp < max_hp * game.LOW_HP_FRACTION:
        buttons.append(Button("Heal", f"{HEAL_PREFIX}{user_id}", style="success", emoji="🧪"))
    return [ActionRow(*buttons)]


def _owner_of(event, prefix) -> str:
    return str(event.get("custom_id") or "")[len(prefix):]


def _player_atk(player) -> int:
    return game.effective_atk(player["sword"], int(player.get("sword_enchant") or 0))


def _player_defense(player) -> int:
    return game.effective_defense(player["armor"], int(player.get("armor_enchant") or 0))


def _gear_label(player, slot) -> str:
    item = game.ITEMS[player[slot]]
    enchant = int(player.get(f"{slot}_enchant") or 0)
    suffix = f" +{enchant}" if enchant else ""
    if slot == "sword":
        return f"{item['emoji']} {item['name']}{suffix} (+{_player_atk(player)} atk)"
    return f"{item['emoji']} {item['name']}{suffix} (+{_player_defense(player)} def)"


def _epoch(player) -> int:
    return int(player.get("rekindles") or 0)


def _add_item(ctx, user_id, item_id, amount=1, epoch=None):
    """Grant items. With `epoch`, the grant only lands if the character hasn't
    Rekindled since that epoch was read — stale grants burn (rowcount 0)."""
    if epoch is None:
        ctx.sql.execute(
            "INSERT INTO inventory (user_id, item_id, qty) VALUES (%s, %s, %s) "
            "ON CONFLICT (user_id, item_id) DO UPDATE SET qty = inventory.qty + %s",
            [user_id, item_id, amount, amount],
        )
        return True
    return ctx.sql.execute(_ADD_ITEM_EPOCH,
                           [user_id, item_id, amount, user_id, epoch, amount]) > 0


def _take_item(ctx, user_id, item_id) -> bool:
    return ctx.sql.execute(_INVENTORY_TAKE, [user_id, item_id]) > 0


def _item_qty(ctx, user_id, item_id) -> int:
    row = ctx.sql.query_one(
        "SELECT qty FROM inventory WHERE user_id = %s AND item_id = %s",
        [user_id, item_id],
    )
    return int(row["qty"]) if row else 0


# --- /start ------------------------------------------------------------------

@plugin.on_slash_command("start")
@_safe
def cmd_start(ctx, event):
    state = _begin(ctx, event, "start", needs_player=False)
    if state is None:
        return
    user_id = str(event.get("user_id"))
    if _get_player(ctx, user_id) is not None:
        _try_respond(ctx, "Your tale is already burning bright — try **/hunt**!")
        return
    now = _now()
    ctx.sql.execute(
        "INSERT INTO players (user_id, username, created_at, last_action_at) "
        "VALUES (%s, %s, %s, %s) ON CONFLICT (user_id) DO NOTHING",
        [user_id, _username(event), now, now],
    )
    ctx.interaction.respond(embeds=[_embed(
        "🔥 Welcome to the Cinderwilds",
        "The old fires are stirring, hero. Here's how to make your name:\n\n"
        "• **/hunt** ash-born monsters for Embers and XP (every 60s)\n"
        "• **/adventure** into the wilds for bigger spoils (every hour)\n"
        "• **/dungeon** — rally 2–4 heroes against a miniboss (level 3+)\n"
        "• **/craft** gear and tonics from dungeon materials, **/enchant** your gear\n"
        "• **/duel** rivals, **/trade** with friends, **/guild** up, fight the daily **/arena**\n"
        "• **/quests** each day, **/open** Ember Caches, raise a **/pet** — and one day, **/rekindle**\n"
        "• **/heal** with healing tonics when the wounds add up\n"
        "• **/shop**, **/buy** and **/sell** to gear up\n"
        "• **/profile**, **/inventory**, and **/equip** to manage your hero\n"
        "• **/daily** for your stipend, **/leaderboard** to see the legends\n"
        "• **/coinflip** if you feel lucky\n\n"
        "HP regenerates slowly on its own. Good hunting!",
        footer=game.VIRTUAL_NOTE,
    )], ephemeral=True)


# --- /profile ----------------------------------------------------------------

@plugin.on_slash_command("profile")
@_safe
def cmd_profile(ctx, event):
    state = _begin(ctx, event, "profile", needs_player=False)
    if state is None:
        return
    target_id = str(_options(event).get("user") or event.get("user_id"))
    player = _get_player(ctx, target_id)
    if player is None:
        if target_id == str(event.get("user_id")):
            _try_respond(ctx, "You haven't begun your tale — run **/start** first!")
        else:
            _try_respond(ctx, "That adventurer hasn't entered the Cinderwilds yet.")
        return
    now = _now()
    player = _apply_regen(player, now)
    xp_in_level, xp_span = game.xp_progress(player["level"], player["xp"])
    name = player["username"] or f"Hero {target_id[-4:]}"
    flames = int(player.get("rekindles") or 0)
    title = f"{name} — Level {player['level']}"
    if flames:
        title += f" 🔥×{flames}"
    fields = [
        {"name": "XP", "value": f"`{game.progress_bar(xp_in_level, xp_span)}` "
                                f"{xp_in_level}/{xp_span}", "inline": True},
        _hp_field(player["hp"], player["max_hp"]),
        {"name": game.CURRENCY, "value": f"🔥 {player['coins']:,}", "inline": True},
        {"name": "Sword", "value": _gear_label(player, "sword"), "inline": True},
        {"name": "Armor", "value": _gear_label(player, "armor"), "inline": True},
    ]
    pet_id = player.get("pet") or ""
    if pet_id in game.PET_PERKS:
        pet = game.ITEMS[pet_id]
        fields.append({"name": "Companion",
                       "value": f"{pet['emoji']} {pet['name']} — "
                                f"{game.PET_PERKS[pet_id]['label']}", "inline": True})
    guild = _my_guild(ctx, target_id)
    if guild:
        fields.append({"name": "Guild", "value": f"🏳️ {guild['name']}", "inline": True})
    season = game.current_season(now)
    footer = (f"{season['emoji']} {_cap(season['name'])} burns — +25% Embers & XP!"
              if season else None)
    ctx.interaction.respond(embeds=[_embed(title, fields=fields, footer=footer)])


# --- /hunt + Hunt again / Heal buttons ----------------------------------------

def _the(name: str) -> str:
    """Article-safe foe naming: mobs are bare ("Cinder Slime"), adventure
    places carry their own article ("the Smoldering Fen") — never 'the the'."""
    return name if name.lower().startswith("the ") else "the " + name


def _loot_field(item_ids):
    lines = [f"{game.ITEMS[i]['emoji']} **{game.ITEMS[i]['name']}**" for i in item_ids]
    return {"name": "Loot", "value": "\n".join(lines), "inline": True}


def _do_hunt(ctx, event, state, *, update_message=False):
    now = _now()
    player = state["player"]
    if not _claim_cooldown(ctx, player, "hunt", "last_hunt_at", game.HUNT_COOLDOWN, now):
        return
    player = _apply_regen(player, now)
    result = game.resolve_hunt(
        player["level"], _player_atk(player), _player_defense(player),
        player["hp"], player["coins"], _rng, state["multiplier"],
        _boosts_for(player, now),
    )
    extra_fields = None
    if result.get("drop"):
        _add_item(ctx, player["user_id"], result["drop"], epoch=_epoch(player))
        extra_fields = [_loot_field([result["drop"]])]
    if result["win"]:
        _quietly(_quest_event, ctx, player["user_id"], "hunt_win", now)
    _finish_encounter(ctx, event, player, result, now, extra_fields, update_message=update_message)


def _finish_encounter(ctx, event, player, result, now, extra_fields=None, *, update_message=False):
    """Apply an encounter result to the player row and respond with the embed."""
    new_xp = player["xp"] + result["xp_gain"]
    new_level = game.level_from_xp(new_xp)
    levels_gained = new_level - player["level"]
    new_max_hp = game.max_hp_for_level(new_level)
    hp_after = new_max_hp if levels_gained > 0 else result["hp_after"]  # level-up fully heals
    anchor = now if levels_gained > 0 else player["_regen_anchor"]
    coins_display = max(0, player["coins"] + result["coins_delta"])

    ctx.sql.execute(
        _ENCOUNTER_UPDATE,
        [_username(event) or player["username"], result["xp_gain"],
         new_level, new_level, new_max_hp, new_max_hp, hp_after,
         result["coins_delta"], result["coins_delta"], anchor, player["user_id"],
         _epoch(player)],
    )

    foe = result["foe"]["name"]
    if result["defeated"]:
        title = f"💀 Knocked out by {_the(foe)}!"
        description = (f"You crawl back with **1 HP**, dropping "
                       f"**{-result['coins_delta']:,} {game.CURRENCY}** in the ash.")
    elif result["win"]:
        title = f"⚔️ You bested {_the(foe)}!"
        description = (f"**+{result['coins_delta']:,} {game.CURRENCY}** · **+{result['xp_gain']} XP**"
                       + (f" · took **{result['damage']}** damage" if result["damage"] else " · unscathed!"))
    else:
        title = f"🏃 {_cap(_the(foe))} drove you off!"
        description = f"No spoils this time — you took **{result['damage']}** damage."
    if result.get("seasonal"):
        description += "\n✨ *A creature of the season — its spoils burn half again as bright!*"

    fields = [_hp_field(hp_after, new_max_hp),
              {"name": game.CURRENCY, "value": f"🔥 {coins_display:,}", "inline": True}]
    if extra_fields:
        fields.extend(extra_fields)
    if levels_gained > 0:
        fields.append({"name": "🎉 LEVEL UP!",
                       "value": f"You reached **Level {new_level}** — max HP is now "
                                f"**{new_max_hp}** and your wounds are healed.",
                       "inline": False})

    ctx.interaction.respond(
        embeds=[_embed(title, description, fields=fields)],
        components=_combat_buttons(player["user_id"], hp_after, new_max_hp),
        update_message=update_message,
    )


@plugin.on_slash_command("hunt")
@_safe
def cmd_hunt(ctx, event):
    state = _begin(ctx, event, "hunt")
    if state is not None:
        _do_hunt(ctx, event, state)


@plugin.on_component(prefix=HUNT_AGAIN_PREFIX)
@_safe
def comp_hunt_again(ctx, event):
    if str(event.get("user_id")) != _owner_of(event, HUNT_AGAIN_PREFIX):
        _try_respond(ctx, "🔥 This isn't your hunt — start your own with **/hunt**.")
        return
    state = _begin(ctx, event, "hunt_again")
    if state is not None:
        # Refresh the combat card in place instead of stacking a new one.
        _do_hunt(ctx, event, state, update_message=True)


@plugin.on_component(prefix=HEAL_PREFIX)
@_safe
def comp_heal(ctx, event):
    if str(event.get("user_id")) != _owner_of(event, HEAL_PREFIX):
        _try_respond(ctx, "🧪 This isn't your satchel — use **/heal** for your own wounds.")
        return
    state = _begin(ctx, event, "heal_button")
    if state is not None:
        _do_heal(ctx, event, state)


# --- /adventure ---------------------------------------------------------------

@plugin.on_slash_command("adventure")
@_safe
def cmd_adventure(ctx, event):
    state = _begin(ctx, event, "adventure")
    if state is None:
        return
    now = _now()
    player = state["player"]
    if not _claim_cooldown(ctx, player, "adventure", "last_adventure_at",
                           game.ADVENTURE_COOLDOWN, now):
        return
    player = _apply_regen(player, now)
    result = game.resolve_adventure(
        player["level"], _player_atk(player), _player_defense(player),
        player["hp"], player["coins"], _rng, state["multiplier"],
        _boosts_for(player, now),
    )
    loot = [i for i in (result.get("drop"), result.get("material")) if i]
    extra_fields = None
    if loot:
        for item_id in loot:
            _add_item(ctx, player["user_id"], item_id, epoch=_epoch(player))
        extra_fields = [_loot_field(loot)]
    if result["win"]:
        _quietly(_quest_event, ctx, player["user_id"], "adventure_win", now)
    _finish_encounter(ctx, event, player, result, now, extra_fields)


# --- /heal --------------------------------------------------------------------

def _do_heal(ctx, event, state):
    now = _now()
    player = _apply_regen(state["player"], now)
    user_id = player["user_id"]
    name = _username(event) or player["username"]
    anchor = player["_regen_anchor"]
    deficit = player["max_hp"] - player["hp"]
    if deficit <= 0:
        _try_respond(ctx, "💚 You're already at full health.")
        return

    tonic = game.ITEMS[game.LIFE_POTION]
    draught = game.ITEMS[game.GREATER_LIFE_POTION]
    small_heal = min(player["max_hp"], player["hp"] + game.LIFE_POTION_HEAL)

    # Source order: tonic from the satchel; a Phoenix Draught only when the
    # deficit is worth it; an 80-Ember purchase; the Draught as last resort.
    if _take_item(ctx, user_id, game.LIFE_POTION):
        hp_after = small_heal
        message = f"You drink an **{tonic['name']}** from your satchel."
        ctx.sql.execute(_HEAL_WITH_POTION, [name, hp_after, anchor, user_id])
    elif deficit > game.LIFE_POTION_HEAL and _take_item(ctx, user_id, game.GREATER_LIFE_POTION):
        hp_after = player["max_hp"]
        message = f"You drain a **{draught['name']}** — every wound closes at once."
        ctx.sql.execute(_HEAL_WITH_POTION, [name, hp_after, anchor, user_id])
    elif ctx.sql.execute(_HEAL_WITH_PURCHASE,
                         [name, small_heal, tonic["price"], anchor, user_id, tonic["price"]]):
        hp_after = small_heal
        message = (f"No tonics left — you buy a fresh **{tonic['name']}** for "
                   f"**{tonic['price']} {game.CURRENCY}** and drink it on the spot.")
    elif _take_item(ctx, user_id, game.GREATER_LIFE_POTION):
        hp_after = player["max_hp"]
        message = f"Down to your last resort: a **{draught['name']}** — every wound closes."
        ctx.sql.execute(_HEAL_WITH_POTION, [name, hp_after, anchor, user_id])
    else:
        _try_respond(ctx, f"No tonics in your satchel, and a fresh {tonic['name']} costs "
                          f"**{tonic['price']} {game.CURRENCY}** — hunt for more Embers first.")
        return

    _quietly(_quest_event, ctx, user_id, "heal", now)
    ctx.interaction.respond(embeds=[_embed(
        "🧪 Healed", message, fields=[_hp_field(hp_after, player["max_hp"])],
    )], ephemeral=True)


@plugin.on_slash_command("heal")
@_safe
def cmd_heal(ctx, event):
    state = _begin(ctx, event, "heal")
    if state is not None:
        _do_heal(ctx, event, state)


# --- /inventory -----------------------------------------------------------------

@plugin.on_slash_command("inventory")
@_safe
def cmd_inventory(ctx, event):
    state = _begin(ctx, event, "inventory")
    if state is None:
        return
    player = state["player"]
    rows = ctx.sql.query(
        "SELECT item_id, qty FROM inventory WHERE user_id = %s AND qty > 0 ORDER BY item_id",
        [player["user_id"]], limit=100,
    )
    lines = []
    for row in rows:
        item = game.ITEMS.get(row["item_id"])
        if item:
            lines.append(f"{item['emoji']} **{item['name']}** × {row['qty']}")
    ctx.interaction.respond(embeds=[_embed(
        "🎒 Your Satchel",
        "\n".join(lines) if lines else "*Nothing but ash and lint.*",
        fields=[
            {"name": "Equipped sword", "value": _gear_label(player, "sword"), "inline": True},
            {"name": "Equipped armor", "value": _gear_label(player, "armor"), "inline": True},
            {"name": game.CURRENCY, "value": f"🔥 {player['coins']:,}", "inline": True},
        ],
    )], ephemeral=True)


# --- /shop + pagination ----------------------------------------------------------

def _shop_response(ctx, page_index, user_id, *, update_message=False):
    pages = game.shop_pages()
    page_index = max(0, min(len(pages) - 1, page_index))
    title, item_ids = pages[page_index]
    lines = []
    for item_id in item_ids:
        item = game.ITEMS[item_id]
        if item["kind"] == "sword":
            stat = f"+{item['atk']} atk"
        elif item["kind"] == "armor":
            stat = f"+{item['defense']} def"
        elif item["kind"] == "lootbox":
            stat = "mystery contents — crack it with /open"
        else:
            stat = "full heal" if item["heal"] is None else f"heals {item['heal']} HP"
        lines.append(f"{item['emoji']} **{item['name']}** — {item['price']:,} {game.CURRENCY} · *{stat}*")
    buttons = [
        Button("◀ Prev", f"{SHOP_PAGE_PREFIX}{page_index - 1}:{user_id}",
               style="secondary", disabled=page_index == 0),
        Button("Next ▶", f"{SHOP_PAGE_PREFIX}{page_index + 1}:{user_id}",
               style="secondary", disabled=page_index == len(pages) - 1),
    ]
    ctx.interaction.respond(
        embeds=[_embed(
            f"🏪 The Emberforge — {title} ({page_index + 1}/{len(pages)})",
            "\n".join(lines) + "\n\nBuy with **/buy item:<name>** · sell back at half price.",
            footer=game.VIRTUAL_NOTE,
        )],
        components=[ActionRow(*buttons)],
        ephemeral=True,
        update_message=update_message,
    )


@plugin.on_slash_command("shop")
@_safe
def cmd_shop(ctx, event):
    state = _begin(ctx, event, "shop", needs_player=False)
    if state is not None:
        _shop_response(ctx, 0, str(event.get("user_id")))


@plugin.on_component(prefix=SHOP_PAGE_PREFIX)
@_safe
def comp_shop_page(ctx, event):
    rest = _owner_of(event, SHOP_PAGE_PREFIX)
    page_str, _, owner = rest.partition(":")
    if str(event.get("user_id")) != owner:
        _try_respond(ctx, "🏪 Browse your own catalog with **/shop**.")
        return
    state = _begin(ctx, event, "shop_page", needs_player=False)
    if state is None:
        return
    try:
        page_index = int(page_str)
    except ValueError:
        page_index = 0
    # Page turns edit the existing shop card in place (0.7.0+) instead of
    # stacking a fresh ephemeral per Prev/Next press.
    _shop_response(ctx, page_index, owner, update_message=True)


# --- /buy & /sell ------------------------------------------------------------------

def _equipped_column(kind):
    return "sword" if kind == "sword" else "armor"


def _equip_fallback(ctx, name, user_id, column, fallback, expected_current) -> bool:
    """Swap a slot to its fallback with ownership proven IN the statement —
    a concurrent disposal can't leave unowned gear equipped. Bare defaults
    are always equippable."""
    if fallback in (game.DEFAULT_SWORD, game.DEFAULT_ARMOR):
        return ctx.sql.execute(_EQUIP_CAS[column],
                               [name, fallback, user_id, expected_current]) > 0
    return ctx.sql.execute(_EQUIP_OWNED_CAS[column],
                           [name, fallback, user_id, expected_current,
                            user_id, fallback]) > 0


def _stat_of(item):
    return item.get("atk", item.get("defense", 0))


@plugin.on_slash_command("buy")
@_safe
def cmd_buy(ctx, event):
    state = _begin(ctx, event, "buy")
    if state is None:
        return
    player = state["player"]
    item_id = game.find_item(_options(event).get("item"))
    if item_id is None or not game.ITEMS[item_id]["price"]:
        _try_respond(ctx, "The Emberforge doesn't stock that — browse **/shop** for what's on offer.")
        return
    item = game.ITEMS[item_id]
    name = _username(event) or player["username"]

    # Debit first (guarded by the balance in the same statement), grant after.
    if not ctx.sql.execute(_COINS_SUB_GUARDED,
                           [name, item["price"], player["user_id"], item["price"]]):
        _try_respond(ctx, f"You're short on {game.CURRENCY}: **{item['name']}** costs "
                          f"**{item['price']:,}**, you carry **{player['coins']:,}**.")
        return
    _add_item(ctx, player["user_id"], item_id, epoch=_epoch(player))

    # Auto-equip only a genuine upgrade: compare against the EFFECTIVE stat
    # (gear + enchant) so a +3 blade isn't overwritten by a worse base item.
    equipped_note = ""
    if item["kind"] in ("sword", "armor"):
        column = _equipped_column(item["kind"])
        current_effective = _player_atk(player) if column == "sword" else _player_defense(player)
        if _stat_of(item) > current_effective:
            if ctx.sql.execute(_EQUIP_CAS[column],
                               [name, item_id, player["user_id"], player[column]]):
                equipped_note = (f"\nYou equip it immediately — it outclasses your "
                                 f"old {item['kind']}.")
    fresh = _get_player(ctx, player["user_id"]) or player
    ctx.interaction.respond(embeds=[_embed(
        f"{item['emoji']} Purchased: {item['name']}",
        f"**-{item['price']:,} {game.CURRENCY}** · you now carry **{fresh['coins']:,}**."
        f"{equipped_note}",
        footer=game.VIRTUAL_NOTE,
    )], ephemeral=True)


@plugin.on_slash_command("sell")
@_safe
def cmd_sell(ctx, event):
    state = _begin(ctx, event, "sell")
    if state is None:
        return
    player = state["player"]
    item_id = game.find_item(_options(event).get("item"))
    if item_id is None or game.sell_price(item_id) <= 0:
        _try_respond(ctx, "That's nothing the Emberforge would pay for.")
        return
    item = game.ITEMS[item_id]
    # Take first (guarded — can't sell what you don't hold), credit after.
    if not _take_item(ctx, player["user_id"], item_id):
        _try_respond(ctx, f"You don't own a **{item['name']}** to sell.")
        return
    price = game.sell_price(item_id)
    name = _username(event) or player["username"]
    ctx.sql.execute(_CREDIT_COINS_EPOCH, [name, price, player["user_id"], _epoch(player)])

    unequip_note = ""
    # The unstrap CAS runs whenever the last copy leaves, WITHOUT consulting
    # the stale row: a concurrent /equip can't keep a sold item's stats.
    if (item["kind"] in ("sword", "armor")
            and _item_qty(ctx, player["user_id"], item_id) <= 0):
        gear_column = _equipped_column(item["kind"])
        owned = ctx.sql.query(
            "SELECT item_id FROM inventory WHERE user_id = %s AND qty > 0",
            [player["user_id"]], limit=100,
        )
        fallback = game.best_gear(item["kind"], [row["item_id"] for row in owned])
        if ctx.sql.execute(_EQUIP_CAS[gear_column],
                           [name, fallback, player["user_id"], item_id]):
            unequip_note = (f"\nThat was your equipped {item['kind']} — you fall back to "
                            f"your {game.ITEMS[fallback]['name']}.")
    elif (item["kind"] == "pet"
            and _item_qty(ctx, player["user_id"], item_id) <= 0):
        if ctx.sql.execute(_CLEAR_PET_CAS, [player["user_id"], item_id]):
            unequip_note = f"\nYour {item['name']} pads off to its new keeper — the leash hangs empty."
    fresh = _get_player(ctx, player["user_id"]) or player
    ctx.interaction.respond(embeds=[_embed(
        f"{item['emoji']} Sold: {item['name']}",
        f"**+{price:,} {game.CURRENCY}** · you now carry **{fresh['coins']:,}**.{unequip_note}",
        footer=game.VIRTUAL_NOTE,
    )], ephemeral=True)


# --- /daily -------------------------------------------------------------------------

@plugin.on_slash_command("daily")
@_safe
def cmd_daily(ctx, event):
    state = _begin(ctx, event, "daily")
    if state is None:
        return
    player = state["player"]
    now = _now()
    if not _claim_cooldown(ctx, player, "daily", "last_daily_at", game.DAILY_COOLDOWN, now):
        return
    reward = game.daily_reward(player["level"], state["multiplier"])
    boosts = _boosts_for(player, now)
    reward["coins"] = max(1, round(reward["coins"] * boosts["coins"]))
    potion = game.ITEMS[reward["item"]]
    ctx.sql.execute(_CREDIT_COINS_EPOCH,
                    [_username(event) or player["username"], reward["coins"],
                     player["user_id"], _epoch(player)])
    _add_item(ctx, player["user_id"], reward["item"], epoch=_epoch(player))
    ctx.interaction.respond(embeds=[_embed(
        "🌅 Daily stipend claimed",
        f"**+{reward['coins']:,} {game.CURRENCY}** and a {potion['emoji']} **{potion['name']}** "
        f"for the road.\nNext claim in **24h**.",
        footer=game.VIRTUAL_NOTE,
    )])


# --- /leaderboard ---------------------------------------------------------------------

@plugin.on_slash_command("leaderboard")
@_safe
def cmd_leaderboard(ctx, event):
    state = _begin(ctx, event, "leaderboard", needs_player=False)
    if state is None:
        return
    metric = str(_options(event).get("metric") or "level").lower()
    if metric not in ("level", "coins", "guild"):
        metric = "level"
    medals = ["🥇", "🥈", "🥉"]
    lines = []
    if metric == "guild":
        rows = ctx.sql.query(
            "SELECT g.name AS name, g.member_count AS members, "
            "COALESCE(SUM(p.level), 0) AS total FROM guilds g "
            "JOIN guild_members m ON m.guild_key = g.key "
            "JOIN players p ON p.user_id = m.user_id "
            "GROUP BY g.key, g.name, g.member_count "
            "ORDER BY total DESC LIMIT 10",
            limit=10,
        )
        if not rows:
            _try_respond(ctx, "No banners fly yet — found one with **/guild action:create**!")
            return
        for i, row in enumerate(rows):
            rank = medals[i] if i < len(medals) else f"`#{i + 1}`"
            lines.append(f"{rank} 🏳️ **{row['name']}** — {row['total']} combined levels "
                         f"· {row['members']} members")
    else:
        if metric == "coins":
            rows = ctx.sql.query(
                "SELECT user_id, username, level, coins, rekindles FROM players "
                "ORDER BY coins DESC LIMIT 10",
                limit=10,
            )
        else:
            rows = ctx.sql.query(
                "SELECT user_id, username, level, xp, coins, rekindles FROM players "
                "ORDER BY level DESC, xp DESC LIMIT 10",
                limit=10,
            )
        if not rows:
            _try_respond(ctx, "No heroes have entered the Cinderwilds yet — be the first with **/start**!")
            return
        for i, row in enumerate(rows):
            rank = medals[i] if i < len(medals) else f"`#{i + 1}`"
            name = row["username"] or f"Hero {str(row['user_id'])[-4:]}"
            flames = f" 🔥×{row['rekindles']}" if int(row.get("rekindles") or 0) else ""
            lines.append(f"{rank} **{name}**{flames} — Level {row['level']} · 🔥 {row['coins']:,}")
    ctx.interaction.respond(embeds=[_embed(
        f"🏆 Legends of the Cinderwilds — by {metric}",
        "\n".join(lines),
        footer=game.VIRTUAL_NOTE,
    )])


# --- /coinflip ---------------------------------------------------------------------------

@plugin.on_slash_command("coinflip")
@_safe
def cmd_coinflip(ctx, event):
    state = _begin(ctx, event, "coinflip")
    if state is None:
        return
    player = state["player"]
    try:
        bet = int(_options(event).get("bet") or 0)
    except (TypeError, ValueError):
        bet = 0
    if bet < game.MIN_BET:
        _try_respond(ctx, f"Minimum wager is **{game.MIN_BET} {game.CURRENCY}**.")
        return
    if player["coins"] < game.MIN_BET:
        _try_respond(ctx, f"You need at least **{game.MIN_BET} {game.CURRENCY}** to flip — "
                          f"earn more with **/hunt**.")
        return
    name = _username(event) or player["username"]
    wager = min(bet, int(player["coins"]))
    # DEBIT-FIRST, like every other wagering flow: the stake leaves the
    # balance before the coin is in the air. A win then credits 2x the
    # actually-debited stake; a loss needs nothing further. (The old shape —
    # credit-on-win against a stale read, cancel-on-failed-debit for losses —
    # had positive expected value under concurrent commands.)
    if not ctx.sql.execute(_COINS_SUB_GUARDED_EPOCH,
                           [name, wager, player["user_id"], wager, _epoch(player)]):
        _try_respond(ctx, "Your Embers shifted before the flip — try again.")
        return
    result = game.coinflip(wager, wager, _rng)
    if result["won"]:
        ctx.sql.execute(_CREDIT_COINS_EPOCH,
                        [name, wager * 2, player["user_id"], _epoch(player)])
    ctx.metrics.record("coinflip_wagered", value=float(wager))
    _quietly(_quest_event, ctx, player["user_id"], "coinflip", _now())
    fresh = _get_player(ctx, player["user_id"]) or player
    clamp_note = ""
    if wager < bet:
        clamp_note = f"\n*(Wager clamped to your balance of {wager:,}.)*"
    if result["won"]:
        title = "🪙 The ember lands glowing-side up!"
        description = (f"You win **+{wager:,} {game.CURRENCY}** — "
                       f"now carrying **{fresh['coins']:,}**.")
    else:
        title = "🪙 The ember lands cold-side up..."
        description = (f"You lose **{wager:,} {game.CURRENCY}** — "
                       f"now carrying **{fresh['coins']:,}**.")
    ctx.interaction.respond(embeds=[_embed(title, description + clamp_note,
                                           footer=game.VIRTUAL_NOTE)])


# --- /dungeon: party expeditions (Phase 2) ---------------------------------------------------

def _broadcast(ctx, channel_id, *, content="", embeds=None):
    """Best-effort channel broadcast (discord:send_message). Never raises:
    a failed announcement must not break the gameplay that triggered it."""
    if not channel_id:
        return
    try:
        ctx.discord.send_message(channel_id=str(channel_id), content=content, embeds=embeds)
    except (SdkError, RuntimeError) as exc:
        ctx.log("EmberQuest broadcast failed: " + str(exc), level="warning")


def _cap(text: str) -> str:
    """Capitalize only the first letter (str.capitalize lowercases the rest,
    mangling proper names like 'the Kilnwarden')."""
    return text[:1].upper() + text[1:]


def _open_lobby(ctx):
    return ctx.sql.query_one("SELECT * FROM dungeons WHERE status = 'open'")


def _lobby_members(ctx, dungeon_id):
    return ctx.sql.query(
        "SELECT user_id, username FROM dungeon_members WHERE dungeon_id = %s "
        "ORDER BY joined_at, user_id",
        [dungeon_id], limit=20,
    )


def _refund_lobby_cooldowns(ctx, dungeon_id):
    """An expired lobby never fought — give every member their 2h claim back."""
    for member in _lobby_members(ctx, dungeon_id):
        ctx.sql.execute(_REFUND_DUNGEON_COOLDOWN, [member["user_id"]])


def _lobby_expired(lobby, now) -> bool:
    return now - int(lobby["created_at"]) >= game.DUNGEON_LOBBY_TTL


def _dungeon_terminal(ctx, lobby_id, statement) -> bool:
    if ctx.sql.execute(statement, [lobby_id]):
        _release_lock(ctx, "dungeon", lobby_id)
        return True
    return False


def _close_lobby_if_stale(ctx, lobby, now) -> bool:
    """Lazily expire a TTL-passed lobby; True if it was (or just went) stale."""
    if lobby["status"] != "open" or not _lobby_expired(lobby, now):
        return lobby["status"] == "expired"
    if _dungeon_terminal(ctx, lobby["id"], _EXPIRE_ONE_LOBBY):
        _refund_lobby_cooldowns(ctx, lobby["id"])
    return True


def _expire_stale_lobbies(ctx, now):
    stale = ctx.sql.query(
        "SELECT id, status, created_at FROM dungeons "
        "WHERE status = 'open' AND created_at < %s",
        [now - game.DUNGEON_LOBBY_TTL], limit=10,
    )
    for lobby in stale:
        _close_lobby_if_stale(ctx, lobby, now)
    # A lobby wedged in 'resolving' (worker died mid-battle) gets closed and
    # its members' 2h claims refunded once it's long past any real fight.
    for lobby in ctx.sql.query(
            "SELECT id FROM dungeons WHERE status = 'resolving' AND created_at < %s",
            [now - game.DUNGEON_LOBBY_TTL - 600], limit=10):
        if _dungeon_terminal(ctx, lobby["id"], _EXPIRE_RESOLVING_LOBBY):
            _quietly(_refund_lobby_cooldowns, ctx, lobby["id"])
    ctx.sql.execute(_PRUNE_DUNGEON_MEMBERS, [now - 7 * 86400])
    ctx.sql.execute(_PRUNE_DUNGEONS, [now - 7 * 86400])


def _claim_player_dungeon_cooldown(ctx, user_id, now) -> bool:
    return ctx.sql.execute(_CLAIM_COOLDOWN["last_dungeon_at"],
                           [now, user_id, now - game.DUNGEON_COOLDOWN]) > 0


def _lobby_response(ctx, lobby, members, now, *, update_message=False):
    dungeon = game.DUNGEONS[lobby["dungeon_key"]]
    roster = "\n".join(f"• **{m['username'] or 'Hero ' + str(m['user_id'])[-4:]}**"
                       for m in members) or "*No heroes yet.*"
    count = max(int(lobby.get("member_count") or 0), len(members))
    remaining = game.DUNGEON_LOBBY_TTL - (now - int(lobby["created_at"]))
    ctx.interaction.respond(
        embeds=[_embed(
            f"🏰 Expedition: {_cap(dungeon['name'])}",
            f"**{_cap(dungeon['boss'])}** awaits a party of "
            f"{game.DUNGEON_MIN_PARTY}–{game.DUNGEON_MAX_PARTY} heroes "
            f"(level {dungeon['min_level']}+).\n"
            f"Press **Join the expedition** below — the assault begins when "
            f"{game.DUNGEON_MAX_PARTY} join, or when a member sounds the horn "
            f"with at least {game.DUNGEON_MIN_PARTY}.",
            fields=[
                {"name": f"Party ({count}/{game.DUNGEON_MAX_PARTY})",
                 "value": roster, "inline": True},
                {"name": "Lobby closes in", "value": game.fmt_duration(remaining), "inline": True},
            ],
            footer=game.VIRTUAL_NOTE,
        )],
        components=[ActionRow(
            Button("Join the expedition", f"{DUNGEON_JOIN_PREFIX}{lobby['id']}",
                   style="success", emoji="🛡️"),
            Button("Sound the horn", f"{DUNGEON_BEGIN_PREFIX}{lobby['id']}",
                   style="danger", emoji="📯"),
        )],
        update_message=update_message,
    )


@plugin.on_slash_command("dungeon")
@_safe
def cmd_dungeon(ctx, event):
    state = _begin(ctx, event, "dungeon")
    if state is None:
        return
    player = state["player"]
    now = _now()
    _expire_stale_lobbies(ctx, now)

    lobby = _open_lobby(ctx)
    if lobby is not None:
        _lobby_response(ctx, lobby, _lobby_members(ctx, lobby["id"]), now)
        return

    # The expiry sweep above may have just refunded this player's cooldown —
    # judge recovery from a fresh row, not the pre-sweep read in _begin.
    player = _get_player(ctx, player["user_id"]) or player

    target = _options(event).get("target")
    dungeon_key = game.pick_dungeon(player["level"], target)
    if dungeon_key is None:
        choices = " · ".join(f"**{d['name']}** (lv {d['min_level']}+)"
                             for d in game.DUNGEONS.values())
        if target:
            _try_respond(ctx, f"No such dungeon. The known depths: {choices}")
        else:
            _try_respond(ctx, f"The depths would swallow you whole — come back at "
                              f"level {min(d['min_level'] for d in game.DUNGEONS.values())}. "
                              f"({choices})")
        return
    dungeon = game.DUNGEONS[dungeon_key]
    if player["level"] < dungeon["min_level"]:
        _try_respond(ctx, f"{_cap(dungeon['name'])} demands level "
                          f"**{dungeon['min_level']}+** — you're not ready yet.")
        return
    recovery = int(player.get("last_dungeon_at") or 0) + game.DUNGEON_COOLDOWN - now
    if recovery > 0:
        _try_respond(ctx, f"⏳ You're still recovering from the last expedition — "
                          f"ready in {game.fmt_duration(recovery)}.")
        return
    spam = ctx.ephemeral.cooldown_check("dungeon:open")
    if spam.get("active"):
        _try_respond(ctx, "A lobby is already being raised — try again in a moment.")
        return
    ctx.ephemeral.cooldown_set("dungeon:open", ttl_seconds=5)

    dungeon_id = str(event.get("interaction_id") or "")
    if not dungeon_id:
        dungeon_id = player["user_id"] + "-" + str(now)  # collision-proof fallback
    channel_id = str(event.get("channel_id") or "")
    # One lobby per server: an atomic lock claim (allowlist-legal stand-in for
    # the partial unique index the host refuses).
    if not _acquire_lock(ctx, "dungeon", "dungeon", dungeon_id, now):
        lobby = _open_lobby(ctx)
        if lobby is not None:
            _lobby_response(ctx, lobby, _lobby_members(ctx, lobby["id"]), now)
        else:
            _try_respond(ctx, "The lobby flickered out — try **/dungeon** again.")
        return
    try:
        opened = ctx.sql.execute(
            _OPEN_DUNGEON, [dungeon_id, dungeon_key, channel_id, player["user_id"], now])
    except (SdkError, RuntimeError):
        # Ambiguous failure: the insert may have committed — releasing the
        # server-wide lock while OUR live lobby row exists would let a second
        # lobby open. Re-read by id and verify ownership (like duel/trade).
        row = ctx.sql.query_one(_DUNGEON_OWNER, [dungeon_id])
        opened = 1 if row and row["leader_id"] == player["user_id"] else 0
    if not opened:  # lost the race — show whoever won
        _release_lock(ctx, "dungeon", dungeon_id)
        lobby = _open_lobby(ctx)
        if lobby is not None:
            _lobby_response(ctx, lobby, _lobby_members(ctx, lobby["id"]), now)
        else:
            _try_respond(ctx, "The lobby flickered out — try **/dungeon** again.")
        return
    # The leader's 2h claim lands after the lobby exists; if it fails (another
    # expedition resolved for them this very instant), take the lobby down.
    if not _claim_player_dungeon_cooldown(ctx, player["user_id"], now):
        _dungeon_terminal(ctx, dungeon_id, _EXPIRE_ONE_LOBBY)
        _try_respond(ctx, "⏳ You're still recovering from the last expedition.")
        return
    ctx.sql.execute(_MEMBER_INSERT,
                    [dungeon_id, player["user_id"],
                     _username(event) or player["username"], now])
    lobby = {"id": dungeon_id, "dungeon_key": dungeon_key, "channel_id": channel_id,
             "leader_id": player["user_id"], "status": "open", "created_at": now,
             "member_count": 1}
    _lobby_response(ctx, lobby, _lobby_members(ctx, dungeon_id), now)


@plugin.on_component(prefix=DUNGEON_JOIN_PREFIX)
@_safe
def comp_dungeon_join(ctx, event):
    state = _begin(ctx, event, "dungeon_join")
    if state is None:
        return
    player = state["player"]
    now = _now()
    dungeon_id = _owner_of(event, DUNGEON_JOIN_PREFIX)
    lobby = ctx.sql.query_one("SELECT * FROM dungeons WHERE id = %s", [dungeon_id])
    if lobby is None or lobby["status"] != "open" or _close_lobby_if_stale(ctx, lobby, now):
        _try_respond(ctx, "🕯️ That expedition's embers have gone cold — open a new one with **/dungeon**.")
        return
    dungeon = game.DUNGEONS[lobby["dungeon_key"]]
    if player["level"] < dungeon["min_level"]:
        _try_respond(ctx, f"{_cap(dungeon['name'])} demands level "
                          f"**{dungeon['min_level']}+** — train up with **/hunt** first.")
        return
    if ctx.sql.query_one(
            "SELECT 1 AS x FROM dungeon_members WHERE dungeon_id = %s AND user_id = %s",
            [dungeon_id, player["user_id"]]):
        _try_respond(ctx, "You're already on the roster — wait for the horn!")
        return
    # Claim the player's 2h window first (atomic — closes the double-dip
    # across back-to-back lobbies); refunded below if no seat was free.
    if not _claim_player_dungeon_cooldown(ctx, player["user_id"], now):
        recovery = int(player.get("last_dungeon_at") or 0) + game.DUNGEON_COOLDOWN - now
        _try_respond(ctx, f"⏳ You're still recovering from the last expedition — "
                          f"ready in {game.fmt_duration(max(recovery, 1))}.")
        return
    if not ctx.sql.execute(_CLAIM_SEAT, [dungeon_id, game.DUNGEON_MAX_PARTY]):
        ctx.sql.execute(_REFUND_DUNGEON_COOLDOWN, [player["user_id"]])
        fresh = ctx.sql.query_one("SELECT status FROM dungeons WHERE id = %s", [dungeon_id])
        _try_respond(ctx, "The party is already full."
                     if fresh and fresh["status"] == "open"
                     else "🕯️ That expedition is already underway or over.")
        return
    username = _username(event) or player["username"]
    if not ctx.sql.execute(_MEMBER_INSERT, [dungeon_id, player["user_id"], username, now]):
        ctx.sql.execute(_RELEASE_SEAT, [dungeon_id])
        _try_respond(ctx, "You're already on the roster — wait for the horn!")
        return

    fresh = ctx.sql.query_one("SELECT member_count FROM dungeons WHERE id = %s", [dungeon_id])
    count = int(fresh["member_count"]) if fresh else len(_lobby_members(ctx, dungeon_id))
    if count >= game.DUNGEON_MAX_PARTY:
        # Full house: this joiner's interaction carries their confirmation; the
        # battle itself is broadcast to the channel (this is what
        # discord:send_message exists for).
        _try_respond(ctx, f"🛡️ You complete the party ({count}/{game.DUNGEON_MAX_PARTY}) — "
                          f"the assault on {dungeon['name']} begins!")
        # Already responded: any failure here must log, not surface a second,
        # contradictory message through _safe.
        try:
            if ctx.sql.execute(_CLAIM_RESOLUTION, [dungeon_id]):
                embed = _resolve_dungeon_safely(ctx, lobby, now, state["multiplier"])
                _broadcast(ctx, lobby["channel_id"], embeds=[embed])
        except (SdkError, RuntimeError) as exc:
            ctx.log("EmberQuest auto-begin failed post-respond: " + str(exc),
                    level="error", request_id=ctx.request_id)
    else:
        # Re-render the shared lobby card in place so its roster and Party (N/5)
        # count update live for everyone; the channel callout still fires.
        updated = {**lobby, "member_count": count}
        _lobby_response(ctx, updated, _lobby_members(ctx, dungeon_id), now,
                        update_message=True)
        _broadcast(ctx, lobby["channel_id"],
                   content=f"🛡️ **{username or 'A hero'}** joined the expedition into "
                           f"{dungeon['name']} — {count}/{game.DUNGEON_MAX_PARTY}.")


@plugin.on_component(prefix=DUNGEON_BEGIN_PREFIX)
@_safe
def comp_dungeon_begin(ctx, event):
    state = _begin(ctx, event, "dungeon_begin")
    if state is None:
        return
    player = state["player"]
    now = _now()
    dungeon_id = _owner_of(event, DUNGEON_BEGIN_PREFIX)
    lobby = ctx.sql.query_one("SELECT * FROM dungeons WHERE id = %s", [dungeon_id])
    if lobby is None or lobby["status"] not in ("open", "resolving"):
        _try_respond(ctx, "🕯️ That expedition is already over.")
        return
    if lobby["status"] == "resolving":
        _try_respond(ctx, "📯 The horn has already sounded — the battle is underway!")
        return
    if _close_lobby_if_stale(ctx, lobby, now):
        _try_respond(ctx, "🕯️ That expedition's embers have gone cold — open a new one with **/dungeon**.")
        return
    member = ctx.sql.query_one(
        "SELECT 1 AS x FROM dungeon_members WHERE dungeon_id = %s AND user_id = %s",
        [dungeon_id, player["user_id"]])
    if member is None:
        _try_respond(ctx, "Only party members may sound the horn — join the expedition first.")
        return
    if int(lobby.get("member_count") or 0) < game.DUNGEON_MIN_PARTY:
        _try_respond(ctx, f"The horn needs a party of at least **{game.DUNGEON_MIN_PARTY}** — "
                          f"rally more heroes first.")
        return
    # Defer BEFORE the claim: if the defer RPC fails, the lobby is still
    # open and recoverable. (_try_respond falls back to followup after defer.)
    ctx.interaction.defer(ephemeral=False)
    if not ctx.sql.execute(_CLAIM_RESOLUTION, [dungeon_id]):
        _try_respond(ctx, "📯 The horn has already sounded — the battle is underway!")
        return
    embed = _resolve_dungeon_safely(ctx, lobby, now, state["multiplier"])
    ctx.interaction.followup(embeds=[embed])


def _resolve_dungeon_safely(ctx, lobby, now, multiplier):
    """Run the resolution behind an error wall: whatever happens, the lobby is
    never wedged in 'resolving' and the caller always gets an embed to show."""
    try:
        return _resolve_dungeon(ctx, lobby, now, multiplier)
    except (SdkError, RuntimeError) as exc:
        ctx.log("EmberQuest dungeon resolution failed: " + str(exc), level="error",
                request_id=ctx.request_id)
        try:
            _dungeon_terminal(ctx, lobby["id"], _FINISH_DUNGEON)
        except (SdkError, RuntimeError):
            pass
        return _embed(
            "💥 The expedition collapses in the dark",
            "Something gave way in the depths — the expedition is over, and the "
            "ash keeps whatever spoils were left unclaimed.",
            footer=game.VIRTUAL_NOTE,
        )


def _resolve_dungeon(ctx, lobby, now, multiplier):
    """Fight the boss, settle every member's row, and return the battle embed."""
    dungeon = game.DUNGEONS[lobby["dungeon_key"]]
    # Roster is read AFTER the resolution claim: the status flip stops any
    # further seat claims, so nobody told "you're in" is left out. A just-
    # claimed seat whose member row hasn't landed yet gets one grace re-read.
    members = _lobby_members(ctx, lobby["id"])
    expected = ctx.sql.query_one("SELECT member_count FROM dungeons WHERE id = %s",
                                 [lobby["id"]])
    if expected and len(members) < int(expected["member_count"]):
        members = _lobby_members(ctx, lobby["id"])
    stats = []
    rows = {}
    for member in members:
        row = _get_player(ctx, member["user_id"])
        if row is None:
            continue
        row = _apply_regen(row, now)
        rows[member["user_id"]] = row
        stats.append({
            "user_id": row["user_id"],
            "username": member["username"] or row["username"],
            "level": row["level"], "atk": _player_atk(row),
            "defense": _player_defense(row), "hp": row["hp"],
        })
    result = game.resolve_dungeon(stats, dungeon, _rng, multiplier)
    # Terminal flip FIRST: a crash mid-loop must not leave 'resolving' for the
    # sweep to void — settled members would get a free cooldown reset on top
    # of kept rewards. Unsettled members lose spoils (burn, never mint).
    _dungeon_terminal(ctx, lobby["id"], _FINISH_DUNGEON)

    fields = []
    for outcome in result["outcomes"]:
        row = rows[outcome["user_id"]]
        new_xp = row["xp"] + outcome["xp"]
        new_level = game.level_from_xp(new_xp)
        levels_gained = new_level - row["level"]
        new_max_hp = game.max_hp_for_level(new_level)
        hp_after = new_max_hp if levels_gained > 0 else outcome["hp_after"]
        anchor = now if levels_gained > 0 else row["_regen_anchor"]
        ctx.sql.execute(
            _DUNGEON_MEMBER_UPDATE,
            [outcome["xp"], new_level, new_level, new_max_hp, new_max_hp,
             hp_after, outcome["coins"], now, anchor, outcome["user_id"],
             _epoch(row)],
        )
        _quietly(_quest_event, ctx, outcome["user_id"], "dungeon", now)
        loot_bits = []
        for item_id, count in outcome["drops"]:
            _add_item(ctx, outcome["user_id"], item_id, count, epoch=_epoch(row))
            item = game.ITEMS[item_id]
            loot_bits.append(f"{item['emoji']}×{count}")
        name = outcome["username"] or f"Hero {str(outcome['user_id'])[-4:]}"
        line = (f"+{outcome['coins']:,} {game.CURRENCY} · +{outcome['xp']} XP · "
                f"took {outcome['damage']} dmg")
        if loot_bits:
            line += " · " + " ".join(loot_bits)
        if outcome["defeated"]:
            line = "💀 dragged out at 1 HP · " + line
        if levels_gained > 0:
            line += f" · 🎉 now level {new_level}!"
        fields.append({"name": name, "value": line, "inline": False})

    ctx.metrics.record("dungeons_resolved",
                       tags={"dungeon": lobby["dungeon_key"], "won": str(result["win"])})

    if result["win"]:
        title = f"⚔️ {_cap(dungeon['boss'])} has fallen!"
        description = (f"The party emerges from {dungeon['name']} victorious — "
                       f"spoils all around!")
    else:
        title = f"💀 {_cap(dungeon['boss'])} repels the expedition!"
        description = (f"The party limps out of {dungeon['name']}. "
                       f"Gear up, enchant, and try again.")
    return _embed(title, description, fields=fields, footer=game.VIRTUAL_NOTE)


# --- /craft (Phase 2) --------------------------------------------------------------------------

def _materials_of(ctx, user_id) -> dict:
    rows = ctx.sql.query(
        "SELECT item_id, qty FROM inventory WHERE user_id = %s AND qty > 0",
        [user_id], limit=100,
    )
    return {row["item_id"]: int(row["qty"]) for row in rows}


def _recipe_line(recipe_id, recipe, owned) -> str:
    item = game.ITEMS[recipe_id]
    bits = []
    craftable = True
    for material_id, needed in recipe["materials"].items():
        have = owned.get(material_id, 0)
        material = game.ITEMS[material_id]
        bits.append(f"{material['emoji']} {have}/{needed}")
        if have < needed:
            craftable = False
    mark = "✅" if craftable else "▫️"
    return (f"{mark} {item['emoji']} **{item['name']}** — {' · '.join(bits)} · "
            f"forge fee {recipe['fee']:,} {game.CURRENCY}")


@plugin.on_slash_command("craft")
@_safe
def cmd_craft(ctx, event):
    state = _begin(ctx, event, "craft")
    if state is None:
        return
    query = _options(event).get("item")
    if not query:
        _craft_list(ctx, state["player"])
        return
    recipe_id = game.find_recipe(query)
    if recipe_id is None:
        _try_respond(ctx, "The forge knows no such recipe — see **/craft** for the catalog.")
        return
    _do_craft(ctx, event, state, recipe_id)


def _craft_list(ctx, player, *, update_message=False, flash=""):
    """Render the crafting board. `flash` prepends a one-line notice (used after
    a forge so the button path re-renders the list with fresh material states)."""
    owned = _materials_of(ctx, player["user_id"])
    lines = [_recipe_line(rid, recipe, owned) for rid, recipe in game.RECIPES.items()]
    buttons = []
    for recipe_id, recipe in game.RECIPES.items():
        craftable = all(owned.get(m, 0) >= n for m, n in recipe["materials"].items())
        buttons.append(Button(
            game.ITEMS[recipe_id]["name"],
            f"{CRAFT_PREFIX}{recipe_id}:{player['user_id']}",
            style="primary" if craftable else "secondary",
            emoji=game.ITEMS[recipe_id]["emoji"], disabled=not craftable,
        ))
    ctx.interaction.respond(
        embeds=[_embed(
            "⚒️ The Emberforge — Crafting",
            flash + "\n".join(lines) + "\n\nMaterials drop from dungeons, adventures, and rare hunts.",
            footer=game.VIRTUAL_NOTE,
        )],
        components=[ActionRow(*buttons)],
        ephemeral=True,
        update_message=update_message,
    )


@plugin.on_component(prefix=CRAFT_PREFIX)
@_safe
def comp_craft(ctx, event):
    rest = _owner_of(event, CRAFT_PREFIX)
    recipe_id, _, owner = rest.partition(":")
    if str(event.get("user_id")) != owner:
        _try_respond(ctx, "⚒️ The forge answers only its patron — use **/craft** yourself.")
        return
    state = _begin(ctx, event, "craft_button")
    if state is None:
        return
    if recipe_id not in game.RECIPES:
        _try_respond(ctx, "The forge knows no such recipe — see **/craft** for the catalog.")
        return
    _do_craft(ctx, event, state, recipe_id, from_component=True)


def _refund_craft_costs(ctx, name, user_id, taken_materials, fee, epoch):
    """Best-effort compensation: put back what was taken. Each step is wrapped
    so one failed refund doesn't abort the rest; everything rides the epoch
    guard so a mid-craft Rekindling burns the costs instead of refunding."""
    for material_id in taken_materials:
        try:
            _add_item(ctx, user_id, material_id, epoch=epoch)
        except (SdkError, RuntimeError):
            pass
    try:
        ctx.sql.execute(_CREDIT_COINS_EPOCH, [name, fee, user_id, epoch])
    except (SdkError, RuntimeError):
        pass


def _do_craft(ctx, event, state, recipe_id, *, from_component=False):
    player = state["player"]
    recipe = game.RECIPES[recipe_id]
    item = game.ITEMS[recipe_id]
    name = _username(event) or player["username"]
    user_id = player["user_id"]

    # Fee first, then guarded material takes; any shortage — or any RPC error
    # mid-sequence — refunds everything taken so far (compensation is additive,
    # so a best-effort replay can never mint value).
    if not ctx.sql.execute(_COINS_SUB_GUARDED, [name, recipe["fee"], user_id, recipe["fee"]]):
        _try_respond(ctx, f"The forge fee for **{item['name']}** is "
                          f"**{recipe['fee']:,} {game.CURRENCY}** — you carry too few.")
        return
    taken = []
    shortage = None
    try:
        for material_id, needed in recipe["materials"].items():
            for _ in range(needed):
                if _take_item(ctx, user_id, material_id):
                    taken.append(material_id)
                else:
                    shortage = material_id
                    break
            if shortage:
                break
    except (SdkError, RuntimeError):
        _refund_craft_costs(ctx, name, user_id, taken, recipe["fee"], _epoch(player))
        raise  # _safe answers the user
    if shortage:
        _refund_craft_costs(ctx, name, user_id, taken, recipe["fee"], _epoch(player))
        needs = " · ".join(f"{game.ITEMS[m]['emoji']} {game.ITEMS[m]['name']} ×{n}"
                           for m, n in recipe["materials"].items())
        _try_respond(ctx, f"Missing materials for **{item['name']}** — it takes {needs}.")
        return

    _add_item(ctx, user_id, recipe_id, epoch=_epoch(player))
    equipped_note = ""
    if item["kind"] in ("sword", "armor"):
        column = _equipped_column(item["kind"])
        current_effective = _player_atk(player) if column == "sword" else _player_defense(player)
        if _stat_of(item) > current_effective:  # don't trample an enchanted upgrade
            if ctx.sql.execute(_EQUIP_CAS[column], [name, recipe_id, user_id, player[column]]):
                equipped_note = ("\nYou equip it straight off the anvil. "
                                 "(Any old enchantment fades.)")
    _quietly(_quest_event, ctx, user_id, "craft", _now())
    consumed = " · ".join(f"{game.ITEMS[m]['emoji']} {game.ITEMS[m]['name']} ×{n}"
                          for m, n in recipe["materials"].items())
    if from_component:
        # Re-render the recipe board in place with updated material/craftable
        # states and a forge banner, so the buttons stay live and accurate.
        fresh = _get_player(ctx, user_id) or player
        _craft_list(
            ctx, fresh, update_message=True,
            flash=f"⚒️ Forged **{item['emoji']} {item['name']}** — consumed {consumed}, "
                  f"**{recipe['fee']:,} {game.CURRENCY}** fee.{equipped_note}\n\n")
        return
    ctx.interaction.respond(embeds=[_embed(
        f"⚒️ Forged: {item['emoji']} {item['name']}",
        f"Consumed {consumed} and a **{recipe['fee']:,} {game.CURRENCY}** forge fee."
        f"{equipped_note}",
        footer=game.VIRTUAL_NOTE,
    )], ephemeral=True)


# --- /enchant (Phase 2) ----------------------------------------------------------------------

@plugin.on_slash_command("enchant")
@_safe
def cmd_enchant(ctx, event):
    state = _begin(ctx, event, "enchant")
    if state is None:
        return
    player = state["player"]
    slot = str(_options(event).get("slot") or "").strip().lower()
    if slot not in ("sword", "armor"):
        _try_respond(ctx, "Choose a slot to enchant: **sword** or **armor**.")
        return
    gear_id = player[slot]
    if gear_id in (game.DEFAULT_SWORD, game.DEFAULT_ARMOR):
        _try_respond(ctx, "Bare fists and traveler's cloth hold no enchantment — "
                          "equip real gear first (**/shop**, **/craft**).")
        return
    current = int(player.get(f"{slot}_enchant") or 0)
    if current >= game.ENCHANT_MAX:
        _try_respond(ctx, f"Your {game.ITEMS[gear_id]['name']} already burns at "
                          f"**+{game.ENCHANT_MAX}** — the metal can hold no more.")
        return

    cost = game.enchant_cost(current + 1)
    shard = game.ITEMS[game.ENCHANT_MATERIAL]
    name = _username(event) or player["username"]
    user_id = player["user_id"]

    if not ctx.sql.execute(_COINS_SUB_GUARDED, [name, cost["coins"], user_id, cost["coins"]]):
        _try_respond(ctx, f"The ritual to **+{current + 1}** costs **{cost['coins']:,} "
                          f"{game.CURRENCY}** and {shard['emoji']} ×{cost['materials']} — "
                          f"you carry too few {game.CURRENCY}.")
        return
    taken = 0
    try:
        while taken < cost["materials"] and _take_item(ctx, user_id, game.ENCHANT_MATERIAL):
            taken += 1
    except (SdkError, RuntimeError):
        _refund_craft_costs(ctx, name, user_id, [game.ENCHANT_MATERIAL] * taken,
                            cost["coins"], _epoch(player))
        raise  # _safe answers the user
    if taken < cost["materials"]:
        _refund_craft_costs(ctx, name, user_id, [game.ENCHANT_MATERIAL] * taken,
                            cost["coins"], _epoch(player))
        _try_respond(ctx, f"The ritual to **+{current + 1}** needs {shard['emoji']} "
                          f"**{shard['name']} ×{cost['materials']}** — you have too few.")
        return

    chance = game.enchant_success_chance(current)
    if game.enchant_attempt(current, _rng):
        if ctx.sql.execute(_ENCHANT_SET[slot], [current + 1, user_id, current, gear_id]):
            stat = "atk" if slot == "sword" else "def"
            bonus = game.ENCHANT_BONUS * (current + 1)
            ctx.interaction.respond(embeds=[_embed(
                f"✨ Enchanted: {game.ITEMS[gear_id]['name']} +{current + 1}",
                f"The runes take hold — **+{bonus} {stat}** from enchantment.\n"
                f"Next ritual: {game.enchant_success_chance(current + 1):.0%} chance, "
                f"{game.enchant_cost(current + 2)['coins']:,} {game.CURRENCY}."
                if current + 1 < game.ENCHANT_MAX else
                f"The runes take hold — **+{bonus} {stat}** from enchantment.\n"
                f"The metal now burns at its limit.",
                footer=game.VIRTUAL_NOTE,
            )], ephemeral=True)
        else:
            # Gear or enchant level changed mid-ritual — void it and refund
            # (epoch-guarded both halves: a mid-ritual Rekindling burns it all).
            _add_item(ctx, user_id, game.ENCHANT_MATERIAL, cost["materials"],
                      epoch=_epoch(player))
            ctx.sql.execute(_CREDIT_COINS_EPOCH,
                            [name, cost["coins"], user_id, _epoch(player)])
            _try_respond(ctx, "The ritual fizzled — your gear shifted mid-incantation. "
                              "Costs returned.")
    else:
        ctx.interaction.respond(embeds=[_embed(
            "💨 The enchantment sputters out",
            f"The runes refuse to bind ({chance:.0%} odds this attempt). "
            f"The {shard['name']}s and fee are spent — the forge keeps its tithe.",
            footer=game.VIRTUAL_NOTE,
        )], ephemeral=True)


# --- /duel: PvP honor bouts (Phase 3) ---------------------------------------------------------

def _social_stats(player) -> dict:
    return {"level": player["level"], "atk": _player_atk(player),
            "defense": _player_defense(player)}


def _settle_social(ctx, player_row, xp_gain, coins_delta=None, duel_time=None) -> int:
    """Award XP (level/max_hp ratchet, no HP changes); returns levels gained."""
    new_xp = player_row["xp"] + xp_gain
    new_level = game.level_from_xp(new_xp)
    new_max_hp = game.max_hp_for_level(new_level)
    if duel_time is not None:
        ctx.sql.execute(_DUEL_SETTLE,
                        [xp_gain, new_level, new_level, new_max_hp, new_max_hp,
                         coins_delta or 0, duel_time, player_row["user_id"],
                         _epoch(player_row)])
    else:
        ctx.sql.execute(_AWARD_XP,
                        [xp_gain, new_level, new_level, new_max_hp, new_max_hp,
                         player_row["user_id"], _epoch(player_row)])
    return new_level - player_row["level"]


def _duel_terminal(ctx, duel, statement) -> bool:
    """Apply a terminal status transition and free the challenger's one-open
    lock; the rowcount keeps every downstream refund exactly-once."""
    if ctx.sql.execute(statement, [duel["id"]]):
        _release_lock(ctx, "duel:" + str(duel["challenger_id"]), duel["id"])
        return True
    return False


def _expire_duel_if_stale(ctx, duel, now) -> bool:
    if duel["status"] != "open" or now - int(duel["created_at"]) < game.DUEL_TTL:
        return duel["status"] in ("expired", "declined", "resolved")
    if _duel_terminal(ctx, duel, _DUEL_EXPIRE_CAS):
        _refund_duel_stake(ctx, duel)  # epoch-guarded against Rekindling
    return True


def _sweep_stale_duels(ctx, now):
    for duel in ctx.sql.query(
            "SELECT * FROM duels WHERE status = 'open' AND created_at < %s",
            [now - game.DUEL_TTL], limit=10):
        _expire_duel_if_stale(ctx, duel, now)
    # A worker death mid-accept can orphan a 'resolving' row; long past any
    # legitimate settlement, void it and send the recorded escrow home
    # (epoch-guarded — refunds burn if the challenger has since Rekindled).
    for duel in ctx.sql.query(_STUCK_RESOLVING_DUELS,
                              [now - game.DUEL_TTL - 600], limit=10):
        if _duel_terminal(ctx, duel, _DUEL_VOID_RESOLVING):
            _quietly(_refund_duel_stake, ctx, duel)
    ctx.sql.execute(_PRUNE_DUELS, [now - 7 * 86400])
    ctx.sql.execute(_PRUNE_LOCKS, [now - 7 * 86400])


def _refund_duel_stake(ctx, duel):
    # Epoch-guarded: a stake escrowed before a Rekindling burns with the rest.
    if int(duel["bet"]) > 0:
        ctx.sql.execute(_CREDIT_COINS_EPOCH,
                        [duel["challenger_name"], duel["bet"], duel["challenger_id"],
                         int(duel.get("epoch") or 0)])


def _reopen_or_void_duel(ctx, duel) -> bool:
    """Put a resolving duel back on the table; if anything refuses, void it
    terminally and return the challenger's stake. True if it reopened."""
    try:
        if ctx.sql.execute(_DUEL_REOPEN, [duel["id"]]):
            return True  # still live: the lock stays held
    except (SdkError, RuntimeError):
        pass
    if _duel_terminal(ctx, duel, _DUEL_VOID_RESOLVING):
        _refund_duel_stake(ctx, duel)
    return False


def _claim_duel_cooldown(ctx, user_id, now) -> bool:
    return ctx.sql.execute(_CLAIM_COOLDOWN["last_duel_at"],
                           [now, user_id, now - game.DUEL_COOLDOWN]) > 0


@plugin.on_slash_command("duel")
@_safe
def cmd_duel(ctx, event):
    state = _begin(ctx, event, "duel")
    if state is None:
        return
    player = state["player"]
    now = _now()
    _sweep_stale_duels(ctx, now)

    options = _options(event)
    target_id = str(options.get("user") or "")
    try:
        bet = max(0, min(int(options.get("bet") or 0), game.DUEL_MAX_BET))
    except (TypeError, ValueError):
        bet = 0
    if not target_id or target_id == player["user_id"]:
        _try_respond(ctx, "Shadow-boxing builds no legend — challenge someone else.")
        return
    target = _get_player(ctx, target_id)
    if target is None:
        _try_respond(ctx, "That adventurer hasn't entered the Cinderwilds yet.")
        return
    recovery = int(player.get("last_duel_at") or 0) + game.DUEL_COOLDOWN - now
    if recovery > 0:
        _try_respond(ctx, f"⏳ Honor needs rest — you can duel again in "
                          f"{game.fmt_duration(recovery)}.")
        return
    name = _username(event) or player["username"]
    # Escrow the challenger's stake up front (epoch-stamped: the debit and its
    # refund share one epoch); refunded on decline/expiry.
    if bet and not ctx.sql.execute(_COINS_SUB_GUARDED_EPOCH,
                                   [name, bet, player["user_id"], bet, _epoch(player)]):
        _try_respond(ctx, f"You can't cover a **{bet:,} {game.CURRENCY}** stake — "
                          f"you carry **{player['coins']:,}**.")
        return
    duel_id = str(event.get("interaction_id") or "")
    if not duel_id:
        duel_id = player["user_id"] + "-" + str(now)  # collision-proof fallback
    target_name = target["username"] or f"Hero {target_id[-4:]}"
    # From here to the insert, the escrow is in flight: ONE wall covers the
    # lock claim and the open so no exception can eat the stake unrefunded.
    lock_held = False
    compensated = False  # set BEFORE each inline refund: if that refund RPC
    # fails ambiguously (it may have committed), the outer wall must NOT
    # refund again — the ambiguity burns instead of minting.
    try:
        # One live challenge per challenger: an atomic lock claim (the host's
        # allowlist forbids CREATE UNIQUE INDEX, so the invariant lives here).
        if not _acquire_lock(ctx, "duel", "duel:" + player["user_id"], duel_id, now):
            if bet:
                compensated = True
                ctx.sql.execute(_CREDIT_COINS_EPOCH,
                                [name, bet, player["user_id"], _epoch(player)])
            _try_respond(ctx, "You already have an open challenge — it must be "
                              "answered or go cold (5m) first.")
            return
        lock_held = True
        try:
            opened = ctx.sql.execute(
                _OPEN_DUEL,
                [duel_id, player["user_id"], target_id, name, target_name,
                 bet, str(event.get("channel_id") or ""), now, _epoch(player),
                 player["user_id"], _epoch(player)])
        except (SdkError, RuntimeError):
            # Ambiguous failure (e.g. a timeout AFTER the host committed):
            # re-read by id — refunding a stake that backs a live row would
            # let its later expiry refund the same stake twice.
            row = ctx.sql.query_one(_DUEL_OWNER, [duel_id])
            opened = 1 if row and row["challenger_id"] == player["user_id"] else 0
        if opened <= 0:
            # Refund through the epoch guard: if the character Rekindled mid-
            # handler, the pre-burn stake stays burned.
            _release_lock(ctx, "duel:" + player["user_id"], duel_id)
            if bet:
                compensated = True
                ctx.sql.execute(_CREDIT_COINS_EPOCH,
                                [name, bet, player["user_id"], _epoch(player)])
            _try_respond(ctx, "The flames shifted mid-challenge — try **/duel** again.")
            return
    except (SdkError, RuntimeError):
        if bet and not compensated:
            _quietly(ctx.sql.execute, _CREDIT_COINS_EPOCH,
                     [name, bet, player["user_id"], _epoch(player)])
        if lock_held:
            _release_lock(ctx, "duel:" + player["user_id"], duel_id)
        raise  # _safe answers the user
    stakes = (f"Stakes: **{bet:,} {game.CURRENCY} each** — winner takes the pot."
              if bet else "An honor bout — no Embers at stake.")
    ctx.interaction.respond(
        content=f"<@{target_id}>",
        embeds=[_embed(
            f"⚔️ {name or 'A hero'} challenges {target_name} to a duel!",
            f"{stakes}\nNo wounds are dealt in a duel — only pride. "
            f"The challenge goes cold in {game.fmt_duration(game.DUEL_TTL)}.",
            footer=game.VIRTUAL_NOTE,
        )],
        components=[ActionRow(
            Button("Accept", f"{DUEL_ACCEPT_PREFIX}{duel_id}", style="success", emoji="⚔️"),
            Button("Decline", f"{DUEL_DECLINE_PREFIX}{duel_id}", style="secondary"),
        )],
    )


@plugin.on_component(prefix=DUEL_ACCEPT_PREFIX)
@_safe
def comp_duel_accept(ctx, event):
    state = _begin(ctx, event, "duel_accept")
    if state is None:
        return
    target = state["player"]  # only the challenged player may accept
    now = _now()
    duel_id = _owner_of(event, DUEL_ACCEPT_PREFIX)
    duel = ctx.sql.query_one("SELECT * FROM duels WHERE id = %s", [duel_id])
    if duel is None or duel["status"] != "open":
        _try_respond(ctx, "🕯️ That challenge is already settled or gone.")
        return
    if _expire_duel_if_stale(ctx, duel, now):
        _try_respond(ctx, "🕯️ That challenge went cold — any stake was returned.")
        return
    if str(event.get("user_id")) != duel["target_id"]:
        _try_respond(ctx, "⚔️ This challenge isn't yours to answer.")
        return
    recovery = int(target.get("last_duel_at") or 0) + game.DUEL_COOLDOWN - now
    if recovery > 0:
        _try_respond(ctx, f"⏳ Honor needs rest — you can duel again in "
                          f"{game.fmt_duration(recovery)}.")
        return
    if not ctx.sql.execute(_DUEL_ACCEPT_CAS, [duel_id]):
        _try_respond(ctx, "⚔️ That duel is already being settled.")
        return
    bet = int(duel["bet"])
    target_name = _username(event) or target["username"]
    target_cd_claimed = False
    challenger_cd_claimed = False
    target_debited = False
    settled = False
    try:
        challenger = _get_player(ctx, duel["challenger_id"])
        if challenger is None:
            _duel_terminal(ctx, duel, _DUEL_FINISH)
            _try_respond(ctx, "Your challenger has vanished into the ash — no duel today.")
            return
        if int(duel.get("epoch") or 0) != _epoch(challenger):
            # The challenger Rekindled since staking: the challenge (and its
            # escrow) belongs to a character that no longer exists. Burn it.
            if _duel_terminal(ctx, duel, _DUEL_VOID_RESOLVING):
                _quietly(_refund_duel_stake, ctx, duel)  # epoch-guarded: burns
            _try_respond(ctx, "🔥 Your challenger's flame has burned out since the "
                              "challenge was made — it dissolves to ash.")
            return
        # Claim BOTH fighters' cooldowns atomically before any money moves —
        # a read-then-set check would let either side double-duel in a race.
        if not _claim_duel_cooldown(ctx, target["user_id"], now):
            still_open = _reopen_or_void_duel(ctx, duel)
            _try_respond(ctx, "⏳ Honor needs rest — you can duel again soon."
                         + ("" if still_open else " The challenge has dissolved; "
                                                  "any stake went home."))
            return
        target_cd_claimed = True
        if not _claim_duel_cooldown(ctx, duel["challenger_id"], now):
            # The challenger spent their honor elsewhere since challenging.
            ctx.sql.execute(_REFUND_DUEL_COOLDOWN, [target["user_id"]])
            if _duel_terminal(ctx, duel, _DUEL_VOID_RESOLVING):
                _refund_duel_stake(ctx, duel)
            _try_respond(ctx, "⚔️ Your challenger already dueled elsewhere — the "
                              "challenge dissolves and any stake goes home.")
            return
        challenger_cd_claimed = True
        if bet and not ctx.sql.execute(_COINS_SUB_GUARDED,
                                       [target_name, bet, target["user_id"], bet]):
            ctx.sql.execute(_REFUND_DUEL_COOLDOWN, [target["user_id"]])
            ctx.sql.execute(_REFUND_DUEL_COOLDOWN, [duel["challenger_id"]])
            still_open = _reopen_or_void_duel(ctx, duel)
            _try_respond(ctx, f"You can't cover the **{bet:,} {game.CURRENCY}** stake yet"
                         + (" — the challenge still stands." if still_open
                            else " — the challenge has dissolved; the stake went home."))
            return
        target_debited = bet > 0

        result = game.resolve_duel(_social_stats(challenger), _social_stats(target), _rng)
        pot = bet * 2
        if result["challenger_wins"]:
            winner, loser = challenger, target
            winner_name = duel["challenger_name"] or challenger["username"]
            loser_name = target_name
        else:
            winner, loser = target, challenger
            winner_name = target_name
            loser_name = duel["challenger_name"] or challenger["username"]
        settled = True  # value starts moving here: never refund past this point
        # Terminal flip FIRST: once 'resolved', no sweep can void-and-refund a
        # pot that the settles below are about to (or already did) move.
        _duel_terminal(ctx, duel, _DUEL_FINISH)
        winner_levels = _settle_social(ctx, winner, game.DUEL_XP_WIN, pot, now)
        _settle_social(ctx, loser, game.DUEL_XP_LOSS, 0, now)
    except (SdkError, RuntimeError):
        if settled:
            _quietly(_duel_terminal, ctx, duel, _DUEL_FINISH)  # pot moved: close it out
        else:
            voided = False
            try:
                voided = _duel_terminal(ctx, duel, _DUEL_VOID_RESOLVING)
            except (SdkError, RuntimeError):
                pass
            if voided:  # each refund is walled on its own — none may block another
                _quietly(_refund_duel_stake, ctx, duel)
                if target_debited:
                    _quietly(ctx.sql.execute, _CREDIT_COINS_EPOCH,
                             [target_name, bet, target["user_id"], _epoch(target)])
                if target_cd_claimed:
                    _quietly(ctx.sql.execute, _REFUND_DUEL_COOLDOWN, [target["user_id"]])
                if challenger_cd_claimed:
                    _quietly(ctx.sql.execute, _REFUND_DUEL_COOLDOWN, [duel["challenger_id"]])
        raise  # _safe answers the user
    ctx.metrics.record("duels_resolved", tags={"wagered": str(bet > 0)})
    _quietly(_quest_event, ctx, duel["challenger_id"], "duel", now)
    _quietly(_quest_event, ctx, target["user_id"], "duel", now)

    odds = result["chance"] if result["challenger_wins"] else 1 - result["chance"]
    description = (f"**{winner_name}** defeats **{loser_name}** "
                   f"({odds:.0%} odds) and claims the **{pot:,} {game.CURRENCY}** pot!"
                   if pot else
                   f"**{winner_name}** defeats **{loser_name}** ({odds:.0%} odds) — "
                   f"glory alone changes hands.")
    if winner_levels > 0:
        description += f"\n🎉 **{winner_name}** reaches level {winner['level'] + winner_levels}!"
    # Rewrite the challenge card into the result and strip the now-dead
    # Accept/Decline buttons (update_message needs SDK >=0.7).
    ctx.interaction.respond(embeds=[_embed("⚔️ The duel is settled!", description,
                                           footer=game.VIRTUAL_NOTE)],
                            update_message=True, components=[])


@plugin.on_component(prefix=DUEL_DECLINE_PREFIX)
@_safe
def comp_duel_decline(ctx, event):
    state = _begin(ctx, event, "duel_decline")
    if state is None:
        return
    now = _now()
    presser = str(event.get("user_id"))
    duel_id = _owner_of(event, DUEL_DECLINE_PREFIX)
    duel = ctx.sql.query_one("SELECT * FROM duels WHERE id = %s", [duel_id])
    if duel is None or duel["status"] != "open" or _expire_duel_if_stale(ctx, duel, now):
        _try_respond(ctx, "🕯️ That challenge is already settled or gone cold.")
        return
    # The target declines; the challenger may withdraw their own challenge.
    if presser not in (duel["target_id"], duel["challenger_id"]):
        _try_respond(ctx, "⚔️ This challenge isn't yours to answer.")
        return
    if not _duel_terminal(ctx, duel, _DUEL_DECLINE_CAS):
        _try_respond(ctx, "⚔️ That duel is already being settled.")
        return
    _refund_duel_stake(ctx, duel)
    if presser == duel["challenger_id"]:
        title = "🕊️ Challenge withdrawn"
        description = (f"**{duel['challenger_name'] or 'The challenger'}** lowers "
                       f"their blade{' — the stake returns home' if int(duel['bet']) else ''}.")
    else:
        title = "🕊️ Challenge declined"
        description = (f"**{duel['target_name']}** walks away — "
                       f"{'the stake returns to ' + (duel['challenger_name'] or 'the challenger') if int(duel['bet']) else 'no harm done'}.")
    # Close the challenge card in place and clear its Accept/Decline buttons.
    ctx.interaction.respond(embeds=[_embed(title, description,
                                           footer=game.VIRTUAL_NOTE)],
                            update_message=True, components=[])


# --- /trade: player-to-player sales (Phase 3) ---------------------------------------------------

def _trade_terminal(ctx, trade, statement) -> bool:
    if ctx.sql.execute(statement, [trade["id"]]):
        _release_lock(ctx, "trade:" + str(trade["seller_id"]), trade["id"])
        return True
    return False


def _refund_trade_escrow(ctx, trade):
    # Epoch-guarded: goods escrowed before a Rekindling burn with the rest.
    returned = _add_item(ctx, trade["seller_id"], trade["item_id"], int(trade["qty"]),
                         epoch=int(trade.get("epoch") or 0))
    if returned and game.ITEMS.get(trade["item_id"], {}).get("kind") == "pet":
        # A companion coming home re-leashes itself if the leash is empty.
        ctx.sql.execute(_ADOPT_IF_PETLESS,
                        [trade["item_id"], trade["seller_id"],
                         trade["seller_id"], trade["item_id"]])


def _expire_trade_if_stale(ctx, trade, now) -> bool:
    if trade["status"] != "open" or now - int(trade["created_at"]) < game.TRADE_TTL:
        return trade["status"] in ("expired", "declined", "cancelled", "resolved")
    if _trade_terminal(ctx, trade, _TRADE_EXPIRE_CAS):
        _refund_trade_escrow(ctx, trade)
    return True


def _sweep_stale_trades(ctx, now):
    for trade in ctx.sql.query(
            "SELECT * FROM trades WHERE status = 'open' AND created_at < %s",
            [now - game.TRADE_TTL], limit=10):
        _expire_trade_if_stale(ctx, trade, now)
    for trade in ctx.sql.query(_STUCK_RESOLVING_TRADES,
                               [now - game.TRADE_TTL - 600], limit=10):
        if _trade_terminal(ctx, trade, _TRADE_VOID_RESOLVING):
            _quietly(_refund_trade_escrow, ctx, trade)
    ctx.sql.execute(_PRUNE_TRADES, [now - 7 * 86400])


def _reopen_or_void_trade(ctx, trade) -> bool:
    """Put a resolving trade back on the table; if anything refuses, void it
    terminally and send the escrowed goods home. True if it reopened."""
    try:
        if ctx.sql.execute(_TRADE_REOPEN, [trade["id"]]):
            return True  # still live: the lock stays held
    except (SdkError, RuntimeError):
        pass
    if _trade_terminal(ctx, trade, _TRADE_VOID_RESOLVING):
        _refund_trade_escrow(ctx, trade)
    return False


@plugin.on_slash_command("trade")
@_safe
def cmd_trade(ctx, event):
    state = _begin(ctx, event, "trade")
    if state is None:
        return
    player = state["player"]
    now = _now()
    _sweep_stale_trades(ctx, now)

    options = _options(event)
    buyer_id = str(options.get("user") or "")
    item_id = game.find_item(options.get("item"))
    try:
        qty = max(1, min(int(options.get("qty") or 1), game.TRADE_MAX_QTY))
    except (TypeError, ValueError):
        qty = 1
    try:
        price = max(0, min(int(options.get("price") or 0), game.TRADE_MAX_PRICE))
    except (TypeError, ValueError):
        price = 0
    if not buyer_id or buyer_id == player["user_id"]:
        _try_respond(ctx, "Trading with your own reflection moves nothing — pick someone else.")
        return
    buyer = _get_player(ctx, buyer_id)
    if buyer is None:
        _try_respond(ctx, "That adventurer hasn't entered the Cinderwilds yet.")
        return
    if item_id is None or game.sell_price(item_id) <= 0:
        _try_respond(ctx, "That's nothing you can trade — sellable gear, tonics, and "
                          "materials only.")
        return
    item = game.ITEMS[item_id]
    name = _username(event) or player["username"]
    # Escrow the goods out of the seller's satchel (epoch-stamped: the take and
    # its refund share one epoch); refunded on every exit path.
    if not ctx.sql.execute(_INVENTORY_TAKE_N_EPOCH,
                           [qty, player["user_id"], item_id, qty,
                            player["user_id"], _epoch(player)]):
        _try_respond(ctx, f"You don't carry **{item['name']} ×{qty}** to offer.")
        return
    unstrap_note = ""
    # Unconditional CAS (no stale-row precondition): offered-away gear or a
    # crated companion must never leave its stats/perk active mid-escrow.
    if (item["kind"] in ("sword", "armor")
            and _item_qty(ctx, player["user_id"], item_id) <= 0):
        column = _equipped_column(item["kind"])
        owned = ctx.sql.query(
            "SELECT item_id FROM inventory WHERE user_id = %s AND qty > 0",
            [player["user_id"]], limit=100,
        )
        fallback = game.best_gear(item["kind"], [row["item_id"] for row in owned])
        if _equip_fallback(ctx, name, player["user_id"], column, fallback, item_id):
            unstrap_note = (f"\n*(You unstrap it first — back to your "
                            f"{game.ITEMS[fallback]['name']}.)*")
    elif (item["kind"] == "pet"
            and _item_qty(ctx, player["user_id"], item_id) <= 0):
        if ctx.sql.execute(_CLEAR_PET_CAS, [player["user_id"], item_id]):
            unstrap_note = "\n*(Your companion waits in the trade crate — perk suspended.)*"
    trade_id = str(event.get("interaction_id") or "")
    if not trade_id:
        trade_id = player["user_id"] + "-" + str(now)  # collision-proof fallback
    lock_held = False
    compensated = False  # see cmd_duel: an ambiguous inline refund must not
    # be refunded a second time by the outer wall
    try:
        if not _acquire_lock(ctx, "trade", "trade:" + player["user_id"], trade_id, now):
            compensated = True
            _add_item(ctx, player["user_id"], item_id, qty, epoch=_epoch(player))
            _try_respond(ctx, "You already have an open offer — cancel it or let it "
                              "lapse (10m) first.")
            return
        lock_held = True
        try:
            opened = ctx.sql.execute(
                _OPEN_TRADE,
                [trade_id, player["user_id"], buyer_id, name, item_id, qty,
                 price, str(event.get("channel_id") or ""), now, _epoch(player),
                 player["user_id"], _epoch(player)])
        except (SdkError, RuntimeError):
            row = ctx.sql.query_one(_TRADE_OWNER, [trade_id])
            opened = 1 if row and row["seller_id"] == player["user_id"] else 0
        if opened <= 0:
            # Epoch-guarded return: goods taken from a since-Rekindled character burn.
            _release_lock(ctx, "trade:" + player["user_id"], trade_id)
            compensated = True
            _add_item(ctx, player["user_id"], item_id, qty, epoch=_epoch(player))
            _try_respond(ctx, "The flames shifted mid-offer — try **/trade** again.")
            return
    except (SdkError, RuntimeError):
        if not compensated:
            _quietly(_add_item, ctx, player["user_id"], item_id, qty, epoch=_epoch(player))
        if lock_held:
            _release_lock(ctx, "trade:" + player["user_id"], trade_id)
        raise  # _safe answers the user
    buyer_name = buyer["username"] or f"Hero {buyer_id[-4:]}"
    deal = (f"**{item['emoji']} {item['name']} ×{qty}** for **{price:,} {game.CURRENCY}**"
            if price else f"**{item['emoji']} {item['name']} ×{qty}** — a gift!")
    ctx.interaction.respond(
        content=f"<@{buyer_id}>",
        embeds=[_embed(
            f"💱 {name or 'A hero'} offers {buyer_name} a trade",
            f"{deal}\nThe offer lapses in {game.fmt_duration(game.TRADE_TTL)}."
            f"{unstrap_note}",
            footer=game.VIRTUAL_NOTE,
        )],
        components=[ActionRow(
            Button("Accept", f"{TRADE_ACCEPT_PREFIX}{trade_id}", style="success", emoji="🤝"),
            Button("Decline", f"{TRADE_DECLINE_PREFIX}{trade_id}", style="secondary"),
            Button("Cancel offer", f"{TRADE_CANCEL_PREFIX}{trade_id}", style="danger"),
        )],
    )


@plugin.on_component(prefix=TRADE_ACCEPT_PREFIX)
@_safe
def comp_trade_accept(ctx, event):
    state = _begin(ctx, event, "trade_accept")
    if state is None:
        return
    buyer = state["player"]
    now = _now()
    trade_id = _owner_of(event, TRADE_ACCEPT_PREFIX)
    trade = ctx.sql.query_one("SELECT * FROM trades WHERE id = %s", [trade_id])
    if trade is None or trade["status"] != "open" or _expire_trade_if_stale(ctx, trade, now):
        _try_respond(ctx, "🕯️ That offer has lapsed or is gone — the goods went home.")
        return
    if str(event.get("user_id")) != trade["buyer_id"]:
        _try_respond(ctx, "💱 This offer isn't addressed to you.")
        return
    if not ctx.sql.execute(_TRADE_ACCEPT_CAS, [trade_id]):
        _try_respond(ctx, "💱 That trade is already being settled.")
        return
    price = int(trade["price"])
    buyer_name = _username(event) or buyer["username"]
    buyer_debited = False
    granted = False
    try:
        seller = _get_player(ctx, trade["seller_id"])
        if seller is None or int(trade.get("epoch") or 0) != _epoch(seller):
            # The seller Rekindled since offering: the goods belong to a burned
            # character. Void the offer before taking the buyer's coins.
            if _trade_terminal(ctx, trade, _TRADE_VOID_RESOLVING):
                _quietly(_refund_trade_escrow, ctx, trade)  # epoch-guarded: burns
            _try_respond(ctx, "🔥 The seller's flame has burned out since this offer "
                              "was made — the goods are gone to ash.")
            return
        if price and not ctx.sql.execute(_COINS_SUB_GUARDED,
                                         [buyer_name, price, buyer["user_id"], price]):
            still_open = _reopen_or_void_trade(ctx, trade)
            _try_respond(ctx, f"You can't cover **{price:,} {game.CURRENCY}** yet"
                         + (" — the offer still stands." if still_open
                            else " — the offer has lapsed; the goods went home."))
            return
        buyer_debited = price > 0
        # Terminal flip FIRST (burn-bias): once 'resolved', neither the sweep
        # nor the wall below can refund escrow for goods being delivered —
        # an ambiguous grant failure burns instead of duplicating.
        granted = True
        _trade_terminal(ctx, trade, _TRADE_FINISH)
        if price:
            ctx.sql.execute(_CREDIT_COINS_EPOCH,
                            [trade["seller_name"], price, trade["seller_id"],
                             int(trade.get("epoch") or 0)])
        _add_item(ctx, buyer["user_id"], trade["item_id"], int(trade["qty"]),
                  epoch=_epoch(buyer))
    except (SdkError, RuntimeError):
        if granted:
            # The goods were delivered: close the trade out, never refund.
            _quietly(_trade_terminal, ctx, trade, _TRADE_FINISH)
        else:
            voided = False
            try:
                voided = _trade_terminal(ctx, trade, _TRADE_VOID_RESOLVING)
            except (SdkError, RuntimeError):
                pass
            if voided:  # each refund is walled on its own — none may block another
                if buyer_debited:
                    _quietly(ctx.sql.execute, _CREDIT_COINS_EPOCH,
                             [buyer_name, price, buyer["user_id"], _epoch(buyer)])
                _quietly(_refund_trade_escrow, ctx, trade)
        raise  # _safe answers the user
    ctx.metrics.record("trades_resolved")
    item = game.ITEMS[trade["item_id"]]
    # Rewrite the offer card into the result and clear its dangling buttons.
    ctx.interaction.respond(embeds=[_embed(
        "🤝 Deal struck!",
        f"**{buyer_name}** takes **{item['emoji']} {item['name']} ×{trade['qty']}**"
        + (f" and **{trade['seller_name']}** pockets **{price:,} {game.CURRENCY}**."
           if price else f" — with **{trade['seller_name']}**'s compliments."),
        footer=game.VIRTUAL_NOTE,
    )], update_message=True, components=[])


@plugin.on_component(prefix=TRADE_DECLINE_PREFIX)
@_safe
def comp_trade_decline(ctx, event):
    state = _begin(ctx, event, "trade_decline")
    if state is None:
        return
    now = _now()
    trade_id = _owner_of(event, TRADE_DECLINE_PREFIX)
    trade = ctx.sql.query_one("SELECT * FROM trades WHERE id = %s", [trade_id])
    if trade is None or trade["status"] != "open" or _expire_trade_if_stale(ctx, trade, now):
        _try_respond(ctx, "🕯️ That offer has already lapsed or closed.")
        return
    if str(event.get("user_id")) != trade["buyer_id"]:
        _try_respond(ctx, "💱 This offer isn't addressed to you.")
        return
    if not _trade_terminal(ctx, trade, _TRADE_DECLINE_CAS):
        _try_respond(ctx, "💱 That trade was already settled elsewhere.")
        return
    _refund_trade_escrow(ctx, trade)
    # Close the offer card in place and clear its Accept/Decline/Cancel buttons.
    ctx.interaction.respond(embeds=[_embed(
        "🕊️ Offer declined",
        "The offer is waved away — the goods return to the seller.",
        footer=game.VIRTUAL_NOTE,
    )], update_message=True, components=[])


@plugin.on_component(prefix=TRADE_CANCEL_PREFIX)
@_safe
def comp_trade_cancel(ctx, event):
    state = _begin(ctx, event, "trade_cancel")
    if state is None:
        return
    now = _now()
    trade_id = _owner_of(event, TRADE_CANCEL_PREFIX)
    trade = ctx.sql.query_one("SELECT * FROM trades WHERE id = %s", [trade_id])
    if trade is None or trade["status"] != "open" or _expire_trade_if_stale(ctx, trade, now):
        _try_respond(ctx, "🕯️ That offer has already lapsed or closed.")
        return
    if str(event.get("user_id")) != trade["seller_id"]:
        _try_respond(ctx, "💱 Only the seller can withdraw this offer.")
        return
    if not _trade_terminal(ctx, trade, _TRADE_CANCEL_CAS):
        _try_respond(ctx, "💱 That trade was already settled elsewhere.")
        return
    _refund_trade_escrow(ctx, trade)
    # Withdraw the offer card in place so the buyer sees it close, not a stale
    # set of live buttons.
    ctx.interaction.respond(embeds=[_embed(
        "🕊️ Offer withdrawn",
        "The seller withdraws the offer — the goods are back in their satchel.",
        footer=game.VIRTUAL_NOTE,
    )], update_message=True, components=[])


# --- /guild (Phase 3) ------------------------------------------------------------------------

def _my_guild(ctx, user_id):
    return ctx.sql.query_one(
        "SELECT g.key AS key, g.name AS name, g.leader_id AS leader_id, "
        "g.member_count AS member_count FROM guild_members m "
        "JOIN guilds g ON g.key = m.guild_key WHERE m.user_id = %s",
        [str(user_id)],
    )


@plugin.on_slash_command("guild")
@_safe
def cmd_guild(ctx, event):
    state = _begin(ctx, event, "guild")
    if state is None:
        return
    options = _options(event)
    action = str(options.get("action") or "info").strip().lower()
    name = options.get("name")
    if action == "create":
        _guild_create(ctx, event, state, name)
    elif action == "join":
        _guild_join(ctx, event, state, name)
    elif action == "leave":
        _guild_leave(ctx, event, state)
    elif action == "info":
        _guild_info(ctx, state, name)
    else:
        _try_respond(ctx, "Guild actions: **create** name:<…> "
                          f"(costs {game.GUILD_CREATE_COST:,} {game.CURRENCY}), "
                          "**join** name:<…>, **leave**, **info** [name].")


def _unwind_guild_shell(ctx, key, founder_id):
    """Compensation for a failed create: delete the empty shell — but if a
    third player already joined it, hand them the banner instead."""
    if ctx.sql.execute("DELETE FROM guilds WHERE key = %s AND leader_id = %s "
                       "AND member_count <= 1", [key, founder_id]):
        return
    ctx.sql.execute(_GUILD_SEAT_RELEASE, [key])  # drop the founder's phantom seat
    _repair_guild_leadership(ctx, key)


def _repair_guild_leadership(ctx, key):
    """Self-healing: if a guild's leader is no longer a member, promote the
    longest-standing member; fold the banner if nobody is left."""
    guild = ctx.sql.query_one("SELECT * FROM guilds WHERE key = %s", [key])
    if guild is None:
        return
    if ctx.sql.query_one(
            "SELECT 1 AS x FROM guild_members WHERE guild_key = %s AND user_id = %s",
            [key, guild["leader_id"]]):
        return  # leader is a member: nothing to repair
    heir = ctx.sql.query_one(
        "SELECT user_id FROM guild_members WHERE guild_key = %s "
        "ORDER BY joined_at, user_id", [key])
    if heir is None:
        ctx.sql.execute(_GUILD_DELETE_EMPTY, [key])
        return
    ctx.sql.execute(_GUILD_TRANSFER, [heir["user_id"], key, guild["leader_id"]])
    # Reconcile drift between the seat counter and the actual roster (a
    # crashed join/leave can skew it; a one-off race here is self-corrected
    # the next time this repair runs).
    count = int(ctx.sql.scalar(
        "SELECT COUNT(*) AS members FROM guild_members WHERE guild_key = %s",
        [key]) or 0)
    ctx.sql.execute("UPDATE guilds SET member_count = %s "
                    "WHERE key = %s AND member_count != %s", [count, key, count])


def _guild_create(ctx, event, state, name):
    player = state["player"]
    error = game.validate_guild_name(name)
    if error:
        _try_respond(ctx, error)
        return
    if _my_guild(ctx, player["user_id"]):
        _try_respond(ctx, "You're already sworn to a guild — **/guild action:leave** first.")
        return
    display = str(name).strip()
    key = game.guild_key(display)
    username = _username(event) or player["username"]
    now = _now()
    if not ctx.sql.execute(_COINS_SUB_GUARDED,
                           [username, game.GUILD_CREATE_COST, player["user_id"],
                            game.GUILD_CREATE_COST]):
        _try_respond(ctx, f"Founding a guild costs **{game.GUILD_CREATE_COST:,} "
                          f"{game.CURRENCY}** — you carry **{player['coins']:,}**.")
        return
    guild_inserted = False
    compensated = False  # an ambiguous inline refund must not be paid twice
    try:
        if not ctx.sql.execute(_GUILD_INSERT, [key, display, player["user_id"], now]):
            compensated = True
            ctx.sql.execute(_CREDIT_COINS_EPOCH,
                            [username, game.GUILD_CREATE_COST, player["user_id"],
                             _epoch(player)])
            _try_respond(ctx, f"A guild already bears the name **{display}**.")
            return
        guild_inserted = True
        if not ctx.sql.execute(_GUILD_MEMBER_INSERT,
                               [player["user_id"], key, username, now]):
            _unwind_guild_shell(ctx, key, player["user_id"])
            compensated = True
            ctx.sql.execute(_CREDIT_COINS_EPOCH,
                            [username, game.GUILD_CREATE_COST, player["user_id"],
                             _epoch(player)])
            _try_respond(ctx, "You're already sworn to a guild — **/guild action:leave** first.")
            return
    except (SdkError, RuntimeError):
        try:
            if guild_inserted:
                _unwind_guild_shell(ctx, key, player["user_id"])
            if not compensated:
                ctx.sql.execute(_CREDIT_COINS_EPOCH,
                                [username, game.GUILD_CREATE_COST, player["user_id"],
                                 _epoch(player)])
        except (SdkError, RuntimeError):
            pass
        raise
    ctx.interaction.respond(embeds=[_embed(
        f"🏳️ {display} is founded!",
        f"**{username or 'A hero'}** raises a new banner over the Cinderwilds.\n"
        f"Recruit with **/guild action:join name:{display}** — up to "
        f"{game.GUILD_MAX_MEMBERS} members.",
        footer=game.VIRTUAL_NOTE,
    )])


def _guild_join(ctx, event, state, name):
    player = state["player"]
    if not name:
        _try_respond(ctx, "Which guild? **/guild action:join name:<guild>**")
        return
    guild = ctx.sql.query_one("SELECT * FROM guilds WHERE key = %s", [game.guild_key(name)])
    if guild is None:
        _try_respond(ctx, "No guild bears that name — **/guild action:create** to found it.")
        return
    if _my_guild(ctx, player["user_id"]):
        _try_respond(ctx, "You're already sworn to a guild — **/guild action:leave** first.")
        return
    if not ctx.sql.execute(_GUILD_SEAT_CLAIM, [guild["key"], game.GUILD_MAX_MEMBERS]):
        _try_respond(ctx, f"**{guild['name']}** is full ({game.GUILD_MAX_MEMBERS} members).")
        return
    username = _username(event) or player["username"]
    try:
        joined = ctx.sql.execute(_GUILD_MEMBER_INSERT,
                                 [player["user_id"], guild["key"], username, _now()])
    except (SdkError, RuntimeError):
        ctx.sql.execute(_GUILD_SEAT_RELEASE, [guild["key"]])  # don't leak the seat
        raise
    if not joined:
        ctx.sql.execute(_GUILD_SEAT_RELEASE, [guild["key"]])
        _try_respond(ctx, "You're already sworn to a guild — **/guild action:leave** first.")
        return
    ctx.interaction.respond(embeds=[_embed(
        f"🏳️ {username or 'A hero'} joins {guild['name']}!",
        f"The banner grows — {int(guild['member_count']) + 1}/{game.GUILD_MAX_MEMBERS} members.",
    )])


def _guild_leave(ctx, event, state):
    player = state["player"]
    mine = _my_guild(ctx, player["user_id"])
    if mine is None:
        _try_respond(ctx, "You're not sworn to any guild.")
        return
    if not ctx.sql.execute(_GUILD_MEMBER_DELETE, [player["user_id"], mine["key"]]):
        _try_respond(ctx, "You're not sworn to any guild.")
        return
    ctx.sql.execute(_GUILD_SEAT_RELEASE, [mine["key"]])
    succession = ""
    if mine["leader_id"] == player["user_id"]:
        heir = ctx.sql.query_one(
            "SELECT user_id, username FROM guild_members WHERE guild_key = %s "
            "ORDER BY joined_at, user_id", [mine["key"]])
        if heir:
            ctx.sql.execute(_GUILD_TRANSFER, [heir["user_id"], mine["key"], player["user_id"]])
            # The heir may have left in the same instant — verify and re-heal.
            _repair_guild_leadership(ctx, mine["key"])
            succession = f" **{heir['username'] or 'The eldest member'}** now carries the banner."
        else:
            ctx.sql.execute(_GUILD_DELETE_EMPTY, [mine["key"]])
            succession = " With no one left, the banner is folded away."
    _try_respond(ctx, f"You leave **{mine['name']}**.{succession}", ephemeral=False)


def _guild_info(ctx, state, name):
    player = state["player"]
    if name:
        guild = ctx.sql.query_one("SELECT * FROM guilds WHERE key = %s",
                                  [game.guild_key(name)])
    else:
        guild = _my_guild(ctx, player["user_id"])
    if guild is None:
        _try_respond(ctx, "No guild found — **/guild action:create name:<…>** to found one, "
                          "or **/guild action:info name:<…>** to inspect another.")
        return
    _repair_guild_leadership(ctx, guild["key"])
    guild = ctx.sql.query_one("SELECT * FROM guilds WHERE key = %s", [guild["key"]]) or guild
    members = ctx.sql.query(
        "SELECT m.user_id AS user_id, m.username AS username, "
        "COALESCE(p.level, 1) AS level FROM guild_members m "
        "LEFT JOIN players p ON p.user_id = m.user_id "
        "WHERE m.guild_key = %s ORDER BY m.joined_at, m.user_id",
        [guild["key"]], limit=game.GUILD_MAX_MEMBERS + 1,
    )
    total_levels = sum(int(m["level"]) for m in members)
    lines = []
    for member in members:
        crown = "👑 " if member["user_id"] == guild["leader_id"] else ""
        lines.append(f"{crown}**{member['username'] or 'Hero ' + str(member['user_id'])[-4:]}** "
                     f"— level {member['level']}")
    ctx.interaction.respond(embeds=[_embed(
        f"🏳️ {guild['name']}",
        "\n".join(lines) or "*An empty banner.*",
        fields=[
            {"name": "Members", "value": f"{len(members)}/{game.GUILD_MAX_MEMBERS}", "inline": True},
            {"name": "Combined level", "value": str(total_levels), "inline": True},
        ],
    )])


# --- /arena: daily tournament (Phase 3) -----------------------------------------------------------

def _resolve_finished_arenas(ctx, today, multiplier):
    """Lazy daily payout: the first arena interaction of a new day settles any
    finished day exactly once (insert-once claim on arena_days)."""
    days = ctx.sql.query(
        "SELECT DISTINCT day FROM arena_entries WHERE day < %s "
        "AND day NOT IN (SELECT day FROM arena_days)",
        [today], limit=5,
    )
    for row in days:
        day = row["day"]
        if not ctx.sql.execute(_ARENA_CLAIM_DAY, [day, _now()]):
            continue
        try:
            _pay_out_arena_day(ctx, day, multiplier)
        except (SdkError, RuntimeError) as exc:
            # The claim is spent; don't let one bad day break the others.
            ctx.log("EmberQuest arena payout failed for " + day + ": " + str(exc),
                    level="error", request_id=ctx.request_id)
    # Settled history has no escrow value — keep the tables bounded.
    cutoff = game.arena_day(_now() - 30 * 86400)
    ctx.sql.execute(_PRUNE_ARENA_ENTRIES, [cutoff])
    ctx.sql.execute(_PRUNE_ARENA_DAYS, [cutoff])


def _pay_out_arena_day(ctx, day, multiplier):
    entries = ctx.sql.query(
        "SELECT * FROM arena_entries WHERE day = %s ORDER BY score DESC, user_id",
        [day], limit=50,
    )
    if not entries:
        return
    total_fees = int(ctx.sql.scalar(
        "SELECT COALESCE(SUM(paid_fee), 0) AS fees FROM arena_entries WHERE day = %s",
        [day]) or 0)
    pool = game.arena_pool(total_fees, multiplier)
    payouts = game.arena_payouts(pool)
    medals = ["🥇", "🥈", "🥉"]
    lines = []
    for i, entry in enumerate(entries[:3]):
        prize = payouts[i] if i < len(payouts) else 0
        if prize:
            try:
                # Deliberately NOT epoch-guarded: a prize is income already
                # earned on the sand — it survives a Rekindling (the confirm
                # screen says so), unlike held coins, which burn.
                ctx.sql.execute(_CREDIT_COINS, [entry["username"], prize, entry["user_id"]])
            except (SdkError, RuntimeError) as exc:
                # One failed payout must not rob the rest of the podium.
                ctx.log("EmberQuest arena prize failed: " + str(exc), level="error")
        champion = entry["username"] or f"Hero {str(entry['user_id'])[-4:]}"
        lines.append(f"{medals[i]} **{champion}** — score {entry['score']} · "
                     f"+{prize:,} {game.CURRENCY}")
    total_entrants = int(ctx.sql.scalar(
        "SELECT COUNT(*) AS entrants FROM arena_entries WHERE day = %s", [day])
        or len(entries))
    ctx.metrics.record("arena_resolved", value=float(total_entrants))
    latest = ctx.sql.query_one(
        "SELECT channel_id FROM arena_entries WHERE day = %s "
        "ORDER BY created_at DESC, user_id", [day])
    _broadcast(ctx, (latest or {}).get("channel_id"), embeds=[_embed(
        f"🏟️ Arena results — {day}",
        "\n".join(lines) + f"\n\n{total_entrants} gladiators · "
                           f"prize pool {pool:,} {game.CURRENCY}.",
        footer=game.VIRTUAL_NOTE,
    )])


def _arena_standings(ctx, today, player, multiplier, just_entered=None):
    top = ctx.sql.query(
        "SELECT username, user_id, score FROM arena_entries WHERE day = %s "
        "ORDER BY score DESC, user_id LIMIT 5",
        [today], limit=5,
    )
    total = int(ctx.sql.scalar(
        "SELECT COUNT(*) AS entries FROM arena_entries WHERE day = %s", [today]) or 0)
    fees = int(ctx.sql.scalar(
        "SELECT COALESCE(SUM(paid_fee), 0) AS fees FROM arena_entries WHERE day = %s",
        [today]) or 0)
    pool = game.arena_pool(fees, multiplier)
    lines = []
    for i, entry in enumerate(top):
        marker = "▸ " if entry["user_id"] == player["user_id"] else ""
        lines.append(f"`#{i + 1}` {marker}**{entry['username'] or 'Hero ' + str(entry['user_id'])[-4:]}**"
                     f" — score {entry['score']}")
    description = (f"You stride onto the sand — your score today: **{just_entered}**.\n\n"
                   if just_entered is not None else "")
    description += "\n".join(lines) if lines else "*The sand lies untouched today.*"
    description += (f"\n\n{total} gladiators so far · prize pool **{pool:,} {game.CURRENCY}** · "
                    f"podium splits {'/'.join(f'{int(s * 100)}%' for s in game.ARENA_SPLITS)}.\n"
                    f"Entry fee: **{game.ARENA_FEE} {game.CURRENCY}** — one entry per hero "
                    f"per day. Results are called after the day turns (UTC).")
    ctx.interaction.respond(
        embeds=[_embed(f"🏟️ The Emberpit Arena — {today}", description,
                       footer=game.VIRTUAL_NOTE)],
        components=[ActionRow(
            Button("Enter the arena", f"{ARENA_JOIN_PREFIX}{today}",
                   style="primary", emoji="🏟️"),
        )],
    )


def _do_arena_enter(ctx, event, state, today):
    player = state["player"]
    now = _now()
    username = _username(event) or player["username"]
    if not ctx.sql.execute(_COINS_SUB_GUARDED,
                           [username, game.ARENA_FEE, player["user_id"], game.ARENA_FEE]):
        _try_respond(ctx, f"The arena's entry fee is **{game.ARENA_FEE} {game.CURRENCY}** — "
                          f"you carry **{player['coins']:,}**.")
        return
    score = (game.arena_score(player["level"], _player_atk(player),
                              _player_defense(player), _rng)
             + int(_boosts_for(player, now)["arena"]))
    if not ctx.sql.execute(_ARENA_ENTER,
                           [today, player["user_id"], username, score, game.ARENA_FEE,
                            str(event.get("channel_id") or ""), now, today]):
        ctx.sql.execute(_CREDIT_COINS_EPOCH,
                        [username, game.ARENA_FEE, player["user_id"], _epoch(player)])
        already = ctx.sql.query_one(
            "SELECT 1 AS x FROM arena_entries WHERE day = %s AND user_id = %s",
            [today, player["user_id"]])
        if already:
            _arena_standings(ctx, today, player, state["multiplier"])
        else:
            # The UTC day flipped (and settled) under our feet — fee returned.
            _try_respond(ctx, "🏟️ The day turned at UTC midnight and that tournament "
                              "has closed — your fee is returned. Run **/arena** again.")
        return
    # The entry is in and counted: award its XP / metrics / quest credit.
    _settle_social(ctx, player, game.ARENA_XP)
    ctx.metrics.record("arena_entries")
    _quietly(_quest_event, ctx, player["user_id"], "arena", now)
    # UTC-midnight TOCTOU: a payout claimed this day in the instant our entry
    # landed. Because _ARENA_ENTER is guarded by "arena_days has no row for today",
    # reaching here means our entry raced a concurrent settle. If that settle's
    # entry-read saw our (already-committed) row, deleting + refunding the fee now
    # would double-pay a podium finisher — or return a fee that funded the pool; if
    # it didn't see us, our fee simply burns. We can't cheaply tell which, so we
    # never refund: the entry stands in the settled tournament (burn-bias — worst
    # case the fee burns, never a mint), and the player is told they made the day.
    if ctx.sql.query_one("SELECT 1 AS x FROM arena_days WHERE day = %s", [today]):
        _try_respond(ctx, "🏟️ The day turned at UTC midnight just as you stepped onto "
                          "the sand — you're counted in today's final results. Run "
                          "**/arena** for the next bracket.")
        return
    _arena_standings(ctx, today, player, state["multiplier"], just_entered=score)


@plugin.on_slash_command("arena")
@_safe
def cmd_arena(ctx, event):
    state = _begin(ctx, event, "arena")
    if state is None:
        return
    now = _now()
    today = game.arena_day(now)
    mine = ctx.sql.query_one(
        "SELECT score FROM arena_entries WHERE day = %s AND user_id = %s",
        [today, state["player"]["user_id"]])
    if mine:
        _arena_standings(ctx, today, state["player"], state["multiplier"])
    else:
        _do_arena_enter(ctx, event, state, today)
    # Lazy payout of finished days runs AFTER the response: up to ~45 RPCs and
    # several broadcasts must never eat into the 3-second respond window.
    _quietly(_resolve_finished_arenas, ctx, today, state["multiplier"])


@plugin.on_component(prefix=ARENA_JOIN_PREFIX)
@_safe
def comp_arena_join(ctx, event):
    state = _begin(ctx, event, "arena_join")
    if state is None:
        return
    now = _now()
    today = game.arena_day(now)
    day = _owner_of(event, ARENA_JOIN_PREFIX)
    if day != today:
        _try_respond(ctx, "🏟️ That day's gates have closed — **/arena** opens today's sand.")
        return
    mine = ctx.sql.query_one(
        "SELECT score FROM arena_entries WHERE day = %s AND user_id = %s",
        [today, state["player"]["user_id"]])
    if mine:
        _try_respond(ctx, f"You're already on the sand today (score **{mine['score']}**) — "
                          f"results come when the day turns.")
    else:
        _do_arena_enter(ctx, event, state, today)
    _quietly(_resolve_finished_arenas, ctx, today, state["multiplier"])


# --- /equip: free gear switching (Phase 4) ---------------------------------------------------

@plugin.on_slash_command("equip")
@_safe
def cmd_equip(ctx, event):
    state = _begin(ctx, event, "equip")
    if state is None:
        return
    player = state["player"]
    item_id = game.find_item(_options(event).get("item"))
    if item_id is None or game.ITEMS[item_id]["kind"] not in ("sword", "armor"):
        _try_respond(ctx, "You can equip swords and armor — see **/inventory** for what you own.")
        return
    item = game.ITEMS[item_id]
    column = _equipped_column(item["kind"])
    if player[column] == item_id:
        _try_respond(ctx, f"Your {item['name']} is already equipped.")
        return
    # The bare defaults are always available; everything else must be owned.
    if (item_id not in (game.DEFAULT_SWORD, game.DEFAULT_ARMOR)
            and _item_qty(ctx, player["user_id"], item_id) < 1):
        _try_respond(ctx, f"You don't own a **{item['name']}** — find one in **/shop**, "
                          f"**/craft**, or a trade.")
        return
    old_enchant = int(player.get(f"{column}_enchant") or 0)
    name = _username(event) or player["username"]
    if item_id in (game.DEFAULT_SWORD, game.DEFAULT_ARMOR):
        swapped = ctx.sql.execute(_EQUIP_CAS[column],
                                  [name, item_id, player["user_id"], player[column]])
    else:
        swapped = ctx.sql.execute(
            _EQUIP_OWNED_CAS[column],
            [name, item_id, player["user_id"], player[column],
             player["user_id"], item_id])
    if not swapped:
        _try_respond(ctx, "Your gear shifted mid-swap — try again.")
        return
    fresh = _get_player(ctx, player["user_id"]) or player
    note = "\n*(The old enchantment fades with the swap.)*" if old_enchant else ""
    ctx.interaction.respond(embeds=[_embed(
        f"{item['emoji']} Equipped: {item['name']}",
        f"Now wielding {_gear_label(fresh, column)}.{note}",
    )], ephemeral=True)


# --- /pet: companions (Phase 4) ----------------------------------------------------------------

def _owned_pets(ctx, user_id):
    owned = _materials_of(ctx, user_id)  # all inventory rows with qty > 0
    return [pid for pid in game.PET_PERKS if owned.get(pid, 0) > 0]


def _pet_list_response(ctx, player, *, update_message=False, flash=""):
    """Render the companions board. `flash` prepends a one-line notice (used
    after a swap so the button path re-renders the list in place)."""
    owned = _owned_pets(ctx, player["user_id"])
    active = player.get("pet") or ""
    lines = []
    buttons = []
    for pet_id in owned:
        pet = game.ITEMS[pet_id]
        marker = "▸ " if pet_id == active else ""
        lines.append(f"{marker}{pet['emoji']} **{pet['name']}** — "
                     f"{game.PET_PERKS[pet_id]['label']}")
        if pet_id != active:
            buttons.append(Button(pet["name"], f"{PET_SET_PREFIX}{pet_id}:{player['user_id']}",
                                  style="secondary", emoji=pet["emoji"]))
    description = ("\n".join(lines) if lines else
                   "*No companions yet — they hatch from Ember Caches (**/open**), "
                   "and the wilds say dungeon caches hold the rarest.*")
    if active and active in game.PET_PERKS:
        description = (f"Walking with you: {game.ITEMS[active]['emoji']} "
                       f"**{game.ITEMS[active]['name']}**\n\n") + description
    ctx.interaction.respond(
        embeds=[_embed("🐾 Your Companions", flash + description)],
        components=[ActionRow(*buttons[:5])] if buttons else None,
        ephemeral=True,
        update_message=update_message,
    )


def _set_active_pet(ctx, event, state, pet_id, *, from_component=False):
    player = state["player"]
    pet = game.ITEMS[pet_id]
    if player.get("pet") == pet_id:
        _try_respond(ctx, f"{pet['emoji']} Your {pet['name']} already trots beside you.")
        return
    # Ownership is proven inside the statement — a companion sliding into a
    # trade crate at this very instant can't end up active on two leashes.
    if not ctx.sql.execute(_SET_PET_OWNED,
                           [pet_id, player["user_id"], player["user_id"], pet_id]):
        _try_respond(ctx, f"No {pet['name']} answers your whistle — companions hatch "
                          f"from Ember Caches (**/open**).")
        return
    if from_component:
        # Re-render the companions list in place with the new active pet and
        # refreshed buttons, keeping the swap flow on one card.
        fresh = _get_player(ctx, player["user_id"]) or player
        _pet_list_response(
            ctx, fresh, update_message=True,
            flash=f"{pet['emoji']} **{pet['name']}** takes the lead — "
                  f"{game.PET_PERKS[pet_id]['label']}.\n\n")
        return
    ctx.interaction.respond(embeds=[_embed(
        f"{pet['emoji']} {pet['name']} takes the lead!",
        f"Perk active: **{game.PET_PERKS[pet_id]['label']}**.",
    )], ephemeral=True)


@plugin.on_slash_command("pet")
@_safe
def cmd_pet(ctx, event):
    state = _begin(ctx, event, "pet")
    if state is None:
        return
    player = state["player"]
    query = _options(event).get("name")
    if query:
        pet_id = game.find_item(query)
        if pet_id is None or game.ITEMS[pet_id]["kind"] != "pet":
            _try_respond(ctx, "No such companion — try **/pet** to see who walks with you.")
            return
        _set_active_pet(ctx, event, state, pet_id)
        return
    _pet_list_response(ctx, player)


@plugin.on_component(prefix=PET_SET_PREFIX)
@_safe
def comp_pet_set(ctx, event):
    rest = _owner_of(event, PET_SET_PREFIX)
    pet_id, _, owner = rest.partition(":")
    if str(event.get("user_id")) != owner:
        _try_respond(ctx, "🐾 That leash isn't yours — see **/pet** for your own companions.")
        return
    state = _begin(ctx, event, "pet_set")
    if state is None:
        return
    if pet_id not in game.PET_PERKS:
        _try_respond(ctx, "No such companion.")
        return
    _set_active_pet(ctx, event, state, pet_id, from_component=True)


# --- /open: Ember Caches (Phase 4) ----------------------------------------------------------------

def _odds_embed():
    """Honest odds for EVERY cache — published before a single Ember is spent."""
    sections = []
    for box_id in game.LOOT_TABLES:
        item = game.ITEMS[box_id]
        rows = "\n".join(f"`{weight:>2}%` {label}"
                         for weight, label in game.lootbox_odds(box_id))
        sections.append(f"**{item['emoji']} {item['name']}**\n{rows}")
    return _embed(
        "📊 Cache odds — what's inside?",
        "\n\n".join(sections) + "\n\nEmber Caches are sold in **/shop** (Curios); "
                                "Blazing Caches drop from dungeon minibosses.",
        footer=game.VIRTUAL_NOTE,
    )


def _do_open(ctx, event, state, box_id, *, from_component=False):
    player = state["player"]
    now = _now()
    box = game.ITEMS[box_id]
    name = _username(event) or player["username"]
    if not _take_item(ctx, player["user_id"], box_id):
        _try_respond(ctx, f"No {box['name']} in your satchel — Ember Caches are sold in "
                          f"**/shop** (Curios).")
        return
    result = game.roll_lootbox(box_id, _rng, _owned_pets(ctx, player["user_id"]))

    public = False
    if result["kind"] == "coins":
        ctx.sql.execute(_CREDIT_COINS_EPOCH,
                        [name, result["amount"], player["user_id"], _epoch(player)])
        if result.get("jackpot"):
            public = True
            description = (f"🎰 **JACKPOT!** The cache overflows: "
                           f"**+{result['amount']:,} {game.CURRENCY}**!")
        elif result.get("duplicate_pet"):
            dup = game.ITEMS[result["duplicate_pet"]]
            description = (f"A {dup['emoji']} {dup['name']} peeks out — but one already "
                           f"walks with you. It leaves **+{result['amount']:,} "
                           f"{game.CURRENCY}** and slips away.")
        else:
            description = f"Embers spill out: **+{result['amount']:,} {game.CURRENCY}**."
    elif result["kind"] == "item":
        _add_item(ctx, player["user_id"], result["item_id"], result["count"],
                  epoch=_epoch(player))
        item = game.ITEMS[result["item_id"]]
        description = f"Inside: {item['emoji']} **{item['name']} ×{result['count']}**."
    else:  # a new companion!
        pet = game.ITEMS[result["item_id"]]
        if not _add_item(ctx, player["user_id"], result["item_id"], epoch=_epoch(player)):
            # The character Rekindled mid-open: the hatchling goes to the void.
            description = "The cache's contents scatter to ash as your flame turns over."
        elif ctx.sql.execute(_TAKE_DUPLICATE_PET,
                             [player["user_id"], result["item_id"]]):
            # CAS on qty > 1: a concurrent hatch of the same companion (or a
            # stale owned-list read) converts exactly one copy — never both.
            ctx.sql.execute(_CREDIT_COINS_EPOCH,
                            [name, game.DUPLICATE_PET_COINS, player["user_id"],
                             _epoch(player)])
            description = (f"A {pet['emoji']} {pet['name']} peeks out — but one already "
                           f"walks with you. It leaves **+{game.DUPLICATE_PET_COINS:,} "
                           f"{game.CURRENCY}** and slips away.")
        else:
            ctx.sql.execute(_ADOPT_IF_PETLESS,
                            [result["item_id"], player["user_id"],
                             player["user_id"], result["item_id"]])
            public = True
            description = (f"🐾 A {pet['emoji']} **{pet['name']}** bounds out of the cache!\n"
                           f"Perk: {game.PET_PERKS[result['item_id']]['label']} — "
                           f"manage companions with **/pet**.")
    ctx.metrics.record("caches_opened", tags={"cache": box_id})

    remaining = _item_qty(ctx, player["user_id"], box_id)
    components = [ActionRow(
        Button(f"Open another ({remaining} left)" if remaining else "Open another",
               f"{LOOT_AGAIN_PREFIX}{box_id}:{player['user_id']}",
               style="primary", emoji=box["emoji"], disabled=remaining <= 0),
        # Odds stay one press away even while caches are owned — disclosure
        # must never require spending the last cache to see it.
        Button("Odds", f"{LOOT_ODDS_PREFIX}{player['user_id']}",
               style="secondary", emoji="📊"),
    )]
    # A reroll from the "Open another" button refreshes the same ephemeral card
    # in place; a public reveal (jackpot / new companion) can't be delivered by
    # editing an ephemeral message, so it posts a fresh public card as before.
    ctx.interaction.respond(
        embeds=[_embed(f"{box['emoji']} {box['name']} creaks open…", description,
                       footer=game.VIRTUAL_NOTE)],
        components=components,
        ephemeral=not public,
        update_message=from_component and not public,
    )


@plugin.on_slash_command("open")
@_safe
def cmd_open(ctx, event):
    state = _begin(ctx, event, "open")
    if state is None:
        return
    player = state["player"]
    query = _options(event).get("item")
    if query:
        if str(query).strip().lower() == "odds":
            ctx.interaction.respond(embeds=[_odds_embed()], ephemeral=True)
            return
        box_id = game.find_item(query)
        if box_id is None or box_id not in game.LOOT_TABLES:
            _try_respond(ctx, "Only caches can be cracked open — **/open item:odds** "
                              "shows the full odds table anytime.")
            return
    else:
        owned = _materials_of(ctx, player["user_id"])
        box_id = next((b for b in game.LOOT_TABLES if owned.get(b, 0) > 0), None)
        if box_id is None:
            ctx.interaction.respond(embeds=[_odds_embed()], ephemeral=True)
            return
    _do_open(ctx, event, state, box_id)


@plugin.on_component(prefix=LOOT_ODDS_PREFIX)
@_safe
def comp_loot_odds(ctx, event):
    if str(event.get("user_id")) != _owner_of(event, LOOT_ODDS_PREFIX):
        _try_respond(ctx, "📊 Ask for your own odds with **/open item:odds**.")
        return
    state = _begin(ctx, event, "loot_odds", needs_player=False)
    if state is None:
        return
    ctx.interaction.respond(embeds=[_odds_embed()], ephemeral=True)


@plugin.on_component(prefix=LOOT_AGAIN_PREFIX)
@_safe
def comp_loot_again(ctx, event):
    rest = _owner_of(event, LOOT_AGAIN_PREFIX)
    box_id, _, owner = rest.partition(":")
    if str(event.get("user_id")) != owner:
        _try_respond(ctx, "📦 That cache isn't yours — buy your own in **/shop**.")
        return
    state = _begin(ctx, event, "open_again")
    if state is None:
        return
    if box_id not in game.LOOT_TABLES:
        _try_respond(ctx, "That cache crumbled to ash.")
        return
    _do_open(ctx, event, state, box_id, from_component=True)


# --- /quests: the daily board (Phase 4) --------------------------------------------------------------

def _quest_board_response(ctx, player, now, *, update_message=False, flash=""):
    """Render today's quest board. `flash` prepends a one-line notice (used after
    a claim so the button path re-renders the board in place — the claimed quest
    strikes through and its button drops, while the others stay live)."""
    day = game.arena_day(now)
    quests = game.daily_quests(player["user_id"], day)
    rows = ctx.sql.query(
        "SELECT quest_idx, progress, claimed FROM quest_progress "
        "WHERE user_id = %s AND day = %s",
        [player["user_id"], day], limit=10,
    )
    by_idx = {int(r["quest_idx"]): r for r in rows}
    boosts = _boosts_for(player, now)

    lines = []
    buttons = []
    for idx, quest in enumerate(quests):
        row = by_idx.get(idx, {})
        progress = min(int(row.get("progress") or 0), quest["target"])
        claimed = int(row.get("claimed") or 0)
        reward = game.quest_reward(quest, player["level"], boosts)
        bar = game.progress_bar(progress, quest["target"], width=6)
        if claimed:
            lines.append(f"✅ ~~{quest['desc']}~~ — claimed")
        elif progress >= quest["target"]:
            lines.append(f"🎁 **{quest['desc']}** — `{bar}` done! "
                         f"Claim **{reward:,} {game.CURRENCY}** + {game.QUEST_XP} XP")
            buttons.append(Button(f"Claim quest {idx + 1}",
                                  f"{QUEST_CLAIM_PREFIX}{idx}:{player['user_id']}",
                                  style="success", emoji="🎁"))
        else:
            lines.append(f"▫️ {quest['desc']} — `{bar}` {progress}/{quest['target']} · "
                         f"{reward:,} {game.CURRENCY}")
    season = game.current_season(now)
    banner = (f"{season['emoji']} **{_cap(season['name'])}** burns until further notice — "
              f"+25% Embers & XP on hunts, adventures, and dailies!\n\n" if season else "")
    ctx.interaction.respond(
        embeds=[_embed(
            f"📜 The Quest Board — {day}",
            flash + banner + "\n".join(lines) + "\n\nThe board redraws when the day turns (UTC).",
            footer=game.VIRTUAL_NOTE,
        )],
        components=[ActionRow(*buttons[:5])] if buttons else None,
        ephemeral=True,
        update_message=update_message,
    )


@plugin.on_slash_command("quests")
@_safe
def cmd_quests(ctx, event):
    state = _begin(ctx, event, "quests")
    if state is None:
        return
    player = state["player"]
    now = _now()
    ctx.sql.execute(_PRUNE_QUESTS, [game.arena_day(now - game.QUEST_PRUNE_DAYS * 86400)])
    _quest_board_response(ctx, player, now)


@plugin.on_component(prefix=QUEST_CLAIM_PREFIX)
@_safe
def comp_quest_claim(ctx, event):
    rest = _owner_of(event, QUEST_CLAIM_PREFIX)
    idx_str, _, owner = rest.partition(":")
    if str(event.get("user_id")) != owner:
        _try_respond(ctx, "📜 That board isn't yours — **/quests** shows your own.")
        return
    state = _begin(ctx, event, "quest_claim")
    if state is None:
        return
    player = state["player"]
    now = _now()
    day = game.arena_day(now)
    quests = game.daily_quests(player["user_id"], day)
    try:
        quest = quests[int(idx_str)]
    except (ValueError, IndexError):
        _try_respond(ctx, "📜 That quest faded with yesterday's board — see **/quests**.")
        return
    # Quest progress deliberately survives a Rekindling: a completed quest is
    # income already earned (the confirm screen says so); the claim below pays
    # at the CURRENT epoch and level, so nothing pre-burn is resurrected.
    if not ctx.sql.execute(_QUEST_CLAIM_CAS,
                           [player["user_id"], day, int(idx_str), quest["target"]]):
        _try_respond(ctx, "📜 Not finished yet — or already claimed. **/quests** has the tally.")
        return
    reward = game.quest_reward(quest, player["level"], _boosts_for(player, now))
    ctx.sql.execute(_CREDIT_COINS_EPOCH,
                    [_username(event) or player["username"], reward, player["user_id"],
                     _epoch(player)])
    _settle_social(ctx, player, game.QUEST_XP)
    ctx.metrics.record("quests_claimed")
    # Re-render the board in place: the claimed quest strikes through and its
    # button drops, the others stay live, and the payout rides a banner so the
    # reward feedback isn't lost when the standalone card goes away.
    fresh = _get_player(ctx, player["user_id"]) or player
    _quest_board_response(
        ctx, fresh, now, update_message=True,
        flash=f"🎁 Claimed *{quest['desc']}* — **+{reward:,} {game.CURRENCY}** and "
              f"**+{game.QUEST_XP} XP**.\n\n")


# --- /rekindle: prestige (Phase 4) ---------------------------------------------------------------------

@plugin.on_slash_command("rekindle")
@_safe
def cmd_rekindle(ctx, event):
    state = _begin(ctx, event, "rekindle")
    if state is None:
        return
    player = state["player"]
    flames = int(player.get("rekindles") or 0)
    bonus_now = round(game.REKINDLE_BONUS_PER * min(flames, game.REKINDLE_BONUS_CAP) * 100)
    if player["level"] < game.REKINDLE_MIN_LEVEL:
        _try_respond(ctx, f"🔥 The heartfire answers only heroes of level "
                          f"**{game.REKINDLE_MIN_LEVEL}+** — you burn at "
                          f"**{player['level']}**."
                          + (f" (Current Rekindling bonus: +{bonus_now}%.)" if flames else ""))
        return
    pet_note = ""
    pet_id = player.get("pet") or ""
    if pet_id in game.PET_PERKS:
        pet_note = (f"\n• Your active companion, "
                    f"{game.ITEMS[pet_id]['emoji']} {game.ITEMS[pet_id]['name']}, "
                    f"**stays at your side**")
    next_bonus = round(game.REKINDLE_BONUS_PER
                       * min(flames + 1, game.REKINDLE_BONUS_CAP) * 100)
    if flames >= game.REKINDLE_BONUS_CAP:
        gain_line = (f"Your bonus already burns at its **+{next_bonus}% cap** — "
                     f"further flames are marks of pride alone, shown beside your name.")
    else:
        gain_line = (f"You gain a permanent flame: **+{next_bonus}% Embers & XP** "
                     f"(stacks to {game.REKINDLE_BONUS_CAP}) and a 🔥 mark beside your name.")
    ctx.interaction.respond(
        embeds=[_embed(
            "🔥 Rekindle your flame?",
            "Returning to the heartfire **cannot be undone**. You will lose:\n"
            "• Level, XP, HP — back to a fresh level 1\n"
            f"• **All {game.CURRENCY}**\n"
            "• Equipped gear and enchantments\n"
            "• Your entire satchel — items, materials, caches, and idle companions\n"
            "• Any open duel challenges or trade offers (withdrawn — their stakes burn too)"
            f"{pet_note}\n"
            "• Kept: your guild, your daily timer, and winnings already earned "
            "(arena prizes, completed quests)\n\n"
            + gain_line,
            footer=game.VIRTUAL_NOTE,
        )],
        components=[ActionRow(Button(
            "Burn it all and begin again",
            f"{REKINDLE_CONFIRM_PREFIX}{player['user_id']}",
            style="danger", emoji="🔥",
        ))],
        ephemeral=True,
    )


def _void_player_escrows(ctx, player):
    """Withdraw the player's open challenges and offers. Runs AFTER a
    successful reset: every refund is epoch-guarded against the row's
    pre-burn epoch, so the stakes burn instead of resurrecting — this just
    closes the rows so counterparties see them as gone."""
    for duel in ctx.sql.query(
            "SELECT * FROM duels WHERE challenger_id = %s AND status = 'open'",
            [player["user_id"]], limit=5):
        if _duel_terminal(ctx, duel, _DUEL_EXPIRE_CAS):
            _quietly(_refund_duel_stake, ctx, duel)
    for trade in ctx.sql.query(
            "SELECT * FROM trades WHERE seller_id = %s AND status = 'open'",
            [player["user_id"]], limit=5):
        if _trade_terminal(ctx, trade, _TRADE_EXPIRE_CAS):
            _quietly(_refund_trade_escrow, ctx, trade)


@plugin.on_component(prefix=REKINDLE_CONFIRM_PREFIX)
@_safe
def comp_rekindle_confirm(ctx, event):
    if str(event.get("user_id")) != _owner_of(event, REKINDLE_CONFIRM_PREFIX):
        _try_respond(ctx, "🔥 That flame isn't yours to rekindle.")
        return
    state = _begin(ctx, event, "rekindle_confirm")
    if state is None:
        return
    player = state["player"]
    now = _now()
    # ONE guarded statement: level gate + rekindle increment land together, so
    # a stale or double-pressed confirm can never fire twice. Nothing is
    # touched before this gate — an ineligible press has zero side effects.
    if not ctx.sql.execute(_REKINDLE_RESET,
                           [now, player["user_id"], game.REKINDLE_MIN_LEVEL]):
        _try_respond(ctx, f"🔥 The fire isn't ready — Rekindling needs level "
                          f"**{game.REKINDLE_MIN_LEVEL}+** (a fresh flame starts at 1).")
        return
    # The companion to spare comes from a FRESH read — the stale pre-reset row
    # could name a pet swapped (or escrowed away) mid-confirmation.
    fresh_keep = _get_player(ctx, player["user_id"]) or player
    keep = fresh_keep.get("pet") or ""
    # The wipe is retried once and fully walled: the reset is already
    # irreversible, so the celebration (and the player's fresh start) must
    # not hinge on one RPC — a doubly-failed wipe is logged loudly instead.
    try:
        ctx.sql.execute(_WIPE_INVENTORY_EXCEPT, [player["user_id"], keep])
    except (SdkError, RuntimeError):
        try:
            ctx.sql.execute(_WIPE_INVENTORY_EXCEPT, [player["user_id"], keep])
        except (SdkError, RuntimeError) as exc:
            ctx.log("EmberQuest rekindle wipe failed twice for "
                    + player["user_id"] + ": " + str(exc), level="error")
    if keep:
        _quietly(ctx.sql.execute, _CLAMP_KEPT_PET, [player["user_id"], keep])
    # Close out open challenges/offers AFTER the burn: their epoch-guarded
    # refunds land in the void, exactly as the warning promised.
    _quietly(_void_player_escrows, ctx, player)
    ctx.metrics.record("rekindled")
    fresh = _get_player(ctx, player["user_id"]) or player
    flames = int(fresh.get("rekindles") or 0)
    bonus = round(game.REKINDLE_BONUS_PER * min(flames, game.REKINDLE_BONUS_CAP) * 100)
    name = _username(event) or player["username"]
    ctx.interaction.respond(embeds=[_embed(
        f"🔥 {name or 'A hero'} rekindles their flame! (×{flames})",
        f"Everything burns away — and from the ash, a brighter ember: "
        f"**+{bonus}% Embers & XP**, forever.\n"
        f"The Cinderwilds await a fresh **/hunt**.",
        footer=game.VIRTUAL_NOTE,
    )])


# --- Dashboard -----------------------------------------------------------------------------

def _scalar_or(ctx, sql, default):
    """Dashboard SQL with a schema-bootstrap retry: widgets can be requested
    before any event has reached this worker (so before on_ready ran here)."""
    try:
        value = ctx.sql.scalar(sql)
    except (SdkError, RuntimeError):
        _ensure_schema(ctx)
        try:
            value = ctx.sql.scalar(sql)
        except (SdkError, RuntimeError):
            return default
    return default if value is None else value


@plugin.on_dashboard("get_player_count")
def dash_player_count(ctx, params):
    return {"value": int(_scalar_or(ctx, "SELECT COUNT(*) AS cnt FROM players", 0)), "change": ""}


@plugin.on_dashboard("get_commands_today")
def dash_commands_today(ctx, params):
    total = int(ctx.metrics.total("commands", period="24h") or 0)
    change = ""
    trend = ctx.metrics.query("commands", period="7d")
    series = trend.get("series") or []
    data = (series[0].get("data") or []) if series else []
    if len(data) >= 2 and data[-2]:
        change = f"{(data[-1] - data[-2]) / data[-2] * 100:+.0f}%"
    return {"value": total, "change": change}


@plugin.on_dashboard("get_economy_total")
def dash_economy_total(ctx, params):
    return {"value": int(_scalar_or(ctx, "SELECT COALESCE(SUM(coins), 0) AS total FROM players", 0)),
            "change": ""}


@plugin.on_dashboard("get_dungeons_cleared")
def dash_dungeons_cleared(ctx, params):
    return {"value": int(ctx.metrics.total("dungeons_resolved", period="30d") or 0),
            "change": ""}


@plugin.on_dashboard("get_duels_fought")
def dash_duels_fought(ctx, params):
    return {"value": int(ctx.metrics.total("duels_resolved", period="30d") or 0),
            "change": ""}


@plugin.on_dashboard("get_caches_opened")
def dash_caches_opened(ctx, params):
    return {"value": int(ctx.metrics.total("caches_opened", period="30d") or 0),
            "change": ""}


@plugin.on_dashboard("get_commands_chart")
def dash_commands_chart(ctx, params):
    trend = ctx.metrics.query("commands", period="7d")
    series = trend.get("series") or [{"name": "Commands", "data": []}]
    return {"labels": trend.get("labels") or [], "series": series}


@plugin.on_dashboard("get_settings")
def dash_get_settings(ctx, params):
    multiplier, channel = _settings(ctx)
    return {"values": {KV_MULTIPLIER: multiplier, KV_CHANNEL: channel}}


@plugin.on_dashboard("save_settings")
def dash_save_settings(ctx, params):
    values = params.get("values") or {}
    current_multiplier, current_channel = _settings(ctx)
    try:
        multiplier = float(values.get(KV_MULTIPLIER))
    except (TypeError, ValueError):
        multiplier = current_multiplier  # keep the old value on unparsable input
    multiplier = min(10.0, max(0.1, multiplier))

    channel = str(values.get(KV_CHANNEL, current_channel) or "").strip()
    # channel_picker degrades to a text field on older renderers — accept a
    # pasted "<#123...>" mention as well as a raw id.
    channel = channel.removeprefix("<#").removesuffix(">").strip()
    if channel and not channel.isdigit():
        return {"error": "Allowed channel must be a channel id (or empty for all channels)."}

    ctx.kv.set(KV_MULTIPLIER, multiplier)
    ctx.kv.set(KV_CHANNEL, channel)
    return {"ok": True}
