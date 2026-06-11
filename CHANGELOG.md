# Changelog

## 0.4.1 — Live-host fixes (2026-06-12)

First production deploy surfaced two host behaviors the SDK doesn't document:

- **The host's SQL allowlist rejects `CREATE UNIQUE INDEX`** (only plain statement types are
  permitted), and the failed statement aborted the rest of the schema bootstrap. Every DDL
  statement is now individually error-walled, and the one-open invariants (dungeon lobby,
  duel challenge, trade offer) moved from partial unique indexes to an allowlist-legal
  **locks table**: atomic `INSERT … ON CONFLICT DO NOTHING` claims, released on every
  terminal transition, with self-healing recovery of locks orphaned by crashed handlers.
- **The live gateway sends `user_username`**, not the `user_name` the SDK's typed schema
  documents — usernames were stored blank. Both fields are now accepted.
- A regression meta-test scans every shipped SQL literal against the host's statement-type
  allowlist so this class of rejection can't return.

## 0.4.0 — Phase 4: Progression & flavor (2026-06-12)

- **Companions** (`/pet`): five pets with distinct passive perks, hatched from caches,
  tradeable; the active companion's perk suspends while it sits in trade escrow.
- **Ember Caches** (`/open`): lootboxes with fully published odds, buyable in the shop's new
  Curios page or dropped by dungeon minibosses; duplicate companions convert to Embers; an
  owner-scoped "Open another" button chains pulls.
- **Daily quests** (`/quests`): three per hero per UTC day — the board is derived
  deterministically from (day, hero), only progress is stored; hooks across hunts, adventures,
  dungeons, duels, crafting, coinflips, healing, and the arena; CAS-gated claim buttons.
- **Rekindling** (`/rekindle`): the prestige loop — at level 20+, a two-step confirmation burns
  level, Embers, gear, enchants, and satchel (the active companion survives) for a permanent
  +10% Embers & XP per flame (caps at five) and 🔥 marks on profile and leaderboard.
- **Seasonal events**: four stateless UTC date-window festivals (+25% rewards) with seasonal
  bonus creatures in hunts; banners on the quest board and profile.
- **`/equip`**: swap to any owned sword/armor freely — ownership proven atomically.
- Dashboard: "Caches opened (30d)" stat card.
- Hardening from adversarial review (20 findings, incl. 1 blocker): `rekindles` is now a
  character **epoch** — every settlement, grant, and escrow refund computed from a stale read
  is epoch-guarded, so nothing can resurrect a freshly-Rekindled character's level or wealth
  (concurrent settles, in-flight escrows, debit-then-grant flows all burn cleanly); /equip and
  /pet prove ownership in-statement; sell/trade unstrap runs unconditionally; duplicate-pet
  conversion re-checks post-grant; cache sourcing copy made true (dungeons now drop caches).

## 0.3.0 — Phase 3: Social (2026-06-11)

- **PvP duels** (`/duel`): equal-stakes Ember wagers with full escrow (challenge → accept),
  target-only Accept/Decline buttons, challenger withdraw, 5m challenge expiry with refunds,
  10m cooldown claimed atomically for both fighters at acceptance.
- **Player trading** (`/trade`): item-for-Embers offers (gifts supported), seller-side item
  escrow — including unstrapping equipped gear — with refunds on decline, cancel, and expiry.
- **Guilds** (`/guild`): founding (2,500 Embers), 20-member cap, leader succession with lazy
  self-healing, guild tags on profiles, and a `guild` leaderboard metric.
- **Daily arena** (`/arena`): one entry per UTC day, 50-Ember fee, podium pays 50/30/20% of the
  pool (+ multiplier-scaled house bonus), results resolved lazily and broadcast on day turnover.
- Dashboard: "Duels fought (30d)" stat card.
- Hardening from adversarial review (24 findings): partial unique indexes now cover the
  `resolving` state, settlement error walls with per-step refund isolation, equipped-gear
  trade unstrap (stat-duplication blocker), arena midnight-race entry gate, per-prize payout
  isolation, guild leadership self-repair, terminal-row pruning.

## 0.2.0 — Phase 2: Combat depth (2026-06-11)

- **Multiplayer dungeons** (`/dungeon`): 2–4 hero lobbies against tiered minibosses, open-to-all
  Join button, auto-begin at full party, channel broadcasts (`discord:send_message`), 2h player
  cooldown claimed at join and refunded if the lobby expires unfought.
- **Crafting** (`/craft`): dungeon materials → tonics and craft-only top gear, with compensating
  refunds on any shortage or failure.
- **Enchanting** (`/enchant`): +1..+5 on equipped gear, rising costs and falling odds, bound to
  the specific item.
- Dashboard: "Dungeons cleared (30d)" stat card.
- Hardening from adversarial review (17 findings): one-lobby unique index, atomic seat claims,
  post-claim roster reads, wedge-proof resolution, effective-stat auto-equip comparisons.

## 0.1.0 — Phase 1: MVP (2026-06-10)

- Core loop: `/start`, `/profile`, `/hunt`, `/adventure`, `/heal`, `/inventory`, `/shop`,
  `/buy`, `/sell`, `/daily`, `/leaderboard`, `/coinflip`.
- Owner-scoped combat buttons, lazy HP regen with banked progress, durable SQL cooldowns,
  per-server economy settings, admin dashboard (stat cards, 7-day chart, settings form).
- Hardening from adversarial review (15 findings): atomic cooldown claims, relative guarded
  coin math, guarded inventory takes, debit-before-grant ordering.
