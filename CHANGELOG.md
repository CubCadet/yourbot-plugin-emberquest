# Changelog

## 0.4.4 — Probe-gated bootstrap (2026-06-12)

The 0.4.3 healing worked in production (the four missing tables landed in the
next hourly window and `/quests` lit up), but the same log proved that **no-op
`ALTER` statements also count against the host's 5/hour DDL budget** — which
made 0.4.3's zero-DDL marker unreachable: it required all 20 statements to
succeed in one pass, and the 8 unconditional ALTERs could never fit the budget.

- **Existence probes**: every DDL statement is now paired with a cheap `SELECT`
  probe; a successful probe proves the statement is a no-op and it is never
  sent. The hourly budget is spent only on DDL that actually changes something.
- A fully-provisioned server now reaches the zero-DDL marker on its **first
  boot** — including servers upgrading from 0.4.2/0.4.3, with no waiting.
- Fresh installs converge faster too: retries no longer re-spend budget
  re-asserting what already landed.

## 0.4.3 — DDL-rate-limit-aware bootstrap (2026-06-12)

Second production deploy surfaced a third undocumented host limit: **"DDL rate
limit: max 5 DDL statements per hour."** The 20-statement schema bootstrap
could not finish in one window, leaving later tables (`quest_progress`,
`arena_entries`, `arena_days`, `locks`) uncreated — `/quests` errored until
enough hourly windows passed.

- **Zero-DDL steady state**: a KV schema-revision marker is written once every
  statement has landed; every boot after that issues no DDL at all, so routine
  restarts can never burn the hourly budget.
- **Priority-ordered provisioning**: tables are created in gameplay order —
  the core loop (`players`, `inventory`), quest bookkeeping, and the `locks`
  gate land in the first hourly window of a fresh install.
- **In-process healing**: while a server's schema is incomplete, the bootstrap
  retries every ~11 minutes from the command preamble, catching the budget
  refill without waiting for a process restart.
- **Honest UX while settling**: commands that hit a not-yet-created table now
  explain that a fresh install raises its grounds over the first few hours,
  instead of a generic error.

## 0.4.2 — Full-system regression audit (2026-06-12)

A seven-lens adversarial regression audit (49 agents, every finding skeptically
verified) confirmed 42 issues across all four phases — all fixed:

- **Coinflip is now debit-first** (blocker): the old shape credited wins against
  a stale balance and cancelled losses penalty-free when the guarded debit
  failed — positive expected value under concurrent commands. The stake now
  leaves the balance before the coin is in the air.
- **Compensation is exactly-once**: inline escrow refunds set a `compensated`
  flag so an ambiguous refund RPC (which may have committed) is never paid a
  second time by the outer error wall (duels, trades, guild founding).
- **Terminal flips land before value moves** (duels, trades, dungeons): a crash
  mid-settlement now burns instead of letting the stuck-'resolving' sweep
  refund escrow that was already paid out.
- Dungeon ambiguous-open now re-reads ownership before releasing the
  server-wide lobby lock; pet hatches honor the epoch-guarded grant result;
  duplicate-pet conversion is a `qty > 1` CAS (concurrent hatches can no longer
  destroy the companion); unstrap fallbacks prove ownership in-statement; the
  Rekindle wipe is retried and walled, and the spared companion survives as
  exactly one copy.
- Arena payouts run after the interaction response (3s window), the
  UTC-midnight entry race unwinds itself with a refund, and result broadcasts
  count all entrants. Dungeons/rosters are pruned; guild member counts
  self-reconcile.
- Copy & docs: "the the <place>" doubled article fixed; cache odds are
  reachable anytime (📊 button + `/open item:odds`); onboarding, perk labels,
  manifest descriptions, README counts/version, and changelog dates corrected.
- Test net expanded to 181 tests: component-dispatch coverage for all 16
  button prefixes, ambiguity ownership re-reads, earned-income epoch
  survivals, quest hooks driven through their host commands, lock self-heal
  age branch and pruning, dashboard drift guard, and a stricter FakeSql that
  mirrors the host's error contract for all sqlite errors.

## 0.4.1 — Live-host fixes (2026-06-11)

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

## 0.4.0 — Phase 4: Progression & flavor (2026-06-11)

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
