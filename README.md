# 🔥 EmberQuest

[![CI](https://github.com/CubCadet/yourbot-plugin-emberquest/actions/workflows/ci.yml/badge.svg)](https://github.com/CubCadet/yourbot-plugin-emberquest/actions/workflows/ci.yml)

An original Discord RPG/economy game for the [YourBot.gg](https://yourbot.gg) plugin platform.
Hunt ash-born monsters in the Cinderwilds, raid dungeon minibosses with your party, duel rivals,
trade with friends, found guilds, fight in the daily arena, craft and enchant gear, and climb your
server's leaderboard — with a real admin dashboard for live stats and a tunable economy.

> **Embers are virtual in-game currency with no real-world value.** All gambling-style features
> (coinflip, duel stakes, arena fees) use Embers only and are labeled as such in-game.

## Features

- **Core loop** — `/hunt` (60s), `/adventure` (1h), `/daily` (24h), lazy HP regen, XP levels,
  a coin economy with shop gear tiers, and unicode HP/XP bars in clean cyan embeds.
- **Interactive combat** — "Hunt again" and low-HP "Heal" buttons, owner-scoped so only you can
  press yours.
- **Multiplayer dungeons** — `/dungeon` raises a party lobby (2–4 heroes) against a tiered
  miniboss. The **Join** button is open to everyone; the assault auto-begins when the party fills.
- **Crafting & enchanting** — materials drop from dungeons (and rare hunts); `/craft` forges
  tonics and craft-only top-tier gear; `/enchant` pushes your equipped gear to +5.
- **PvP duels** — `/duel` with optional equal-stakes Ember wagers, escrowed and refunded on
  decline/expiry. Honor bouts: no HP lost, winner takes the pot.
- **Player trading** — `/trade` offers items to a specific player for Embers (or as a gift),
  with full escrow on every exit path.
- **Guilds** — found a banner (2,500 Embers), recruit up to 20, leader succession, guild
  leaderboard by combined levels.
- **Daily arena** — one entry per UTC day, podium pays 50/30/20% of the prize pool, results
  broadcast automatically when the day turns.
- **Admin dashboard** — player/command/economy stat cards, a 7-day activity chart, and a
  settings form (economy multiplier, channel restriction) — no bot restarts needed.

## Commands

| Command | What it does |
| --- | --- |
| `/start` | Begin your adventure (one-time onboarding) |
| `/profile [user]` | Level, XP/HP bars, Embers, gear, guild |
| `/hunt` | Fight a monster for Embers and XP (60s cooldown) |
| `/adventure` | Bigger rewards, bigger risk (1h cooldown) |
| `/heal` | Drink a tonic (auto-buys one if needed) |
| `/inventory` | Your satchel and equipped gear |
| `/shop` | Browse the Emberforge (paginated) |
| `/buy item:` / `/sell item:` | Trade with the shop; gear auto-equips on upgrade |
| `/daily` | Daily Ember stipend + a tonic (24h, survives restarts) |
| `/leaderboard [metric]` | Top 10 by `level`, `coins`, or `guild` |
| `/coinflip bet:` | Fair 50/50 flip for Embers |
| `/dungeon [target]` | Open or view a 2–4 hero expedition (2h cooldown, level 3+) |
| `/craft [item]` | List recipes or forge one from materials |
| `/enchant slot:` | Enchant your equipped sword or armor (+5 max) |
| `/duel user: [bet:]` | Challenge a rival, optionally for an Ember stake (10m cooldown) |
| `/trade user: item: price: [qty:]` | Offer items to a player for Embers (0 = gift) |
| `/guild action: [name:]` | `create`, `join`, `leave`, or `info` |
| `/arena` | Enter today's tournament (50-Ember fee, daily) |

## Requirements

- A [YourBot.gg](https://yourbot.gg) server installation.
- Capabilities requested: `interaction:respond`, `storage:kv`, `storage:sql`,
  `discord:send_message` — the minimum for slash commands, settings, durable game state, and
  dungeon/arena broadcasts. Nothing else.

## Development

Built against **yourbot-sdk 0.6.1**. Everything runs locally — no Docker, no Discord connection.

```bash
uv venv .venv
uv pip install --python .venv/bin/python "yourbot-sdk>=0.6.1,<0.7" pytest

# run the test suite (134 tests)
.venv/bin/python -m pytest tests/ -q

# validate against the platform's pre-submit checks
.venv/bin/yourbot validate --path .

# regenerate the dashboard manifest after editing tests/gen_dashboard.py
.venv/bin/python tests/gen_dashboard.py
```

### Project layout

```
├── manifest.json            # plugin id/version, capabilities, 19 slash commands
├── __main__.py              # sandbox entry point
├── handlers.py              # all slash/component/lifecycle/dashboard handlers
├── game.py                  # pure game logic: catalogs, RNG math, balance (no SDK imports)
├── dashboard_manifest.json  # generated — do not edit by hand
├── requirements.txt
└── tests/
    ├── test_plugin.py       # MockContext + sqlite-backed SQL shim
    ├── gen_dashboard.py     # dashboard manifest generator (yourbot_sdk.dashboard DSL)
    └── conftest.py
```

### Engineering notes

The platform dispatches events on multiple threads with no SQL transactions, so the plugin is
built around single-statement atomicity:

- Every gate (cooldowns, dungeon seats, duel/trade escrow, arena entries) is a **conditional
  `UPDATE`/`INSERT` judged by its rowcount** — never read-then-act.
- Money and items move **debit-first, grant-after**; a mid-sequence failure can cost a player a
  reward but can never mint value. Escrows refund through CAS status transitions so each refund
  happens exactly once, with every compensation step independently error-walled.
- One-open invariants (dungeon lobby, duel challenge, trade offer) are enforced with **partial
  unique indexes** whose predicates cover the transient `resolving` state.
- No background jobs: cooldowns, lobby expiry, and arena payouts all resolve **lazily** on the
  next relevant interaction.

### Packaging a release

```bash
.venv/bin/python - <<'EOF'
import zipfile, json
from pathlib import Path
ROOT = Path(".")
version = json.loads((ROOT / "manifest.json").read_text())["version"]
out = ROOT / "dist" / f"emberquest-{version}.zip"
out.parent.mkdir(exist_ok=True)
with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
    for name in ["manifest.json", "__main__.py", "handlers.py", "game.py",
                 "dashboard_manifest.json", "requirements.txt"]:
        zf.write(ROOT / name, name)
print(out)
EOF
```

Upload the zip on the [YourBot Dev Portal](https://yourbot.gg/dev) (contents must sit at the zip
root). Review turnaround is typically 1–3 business days.

## Versions

See [CHANGELOG.md](CHANGELOG.md). Current: **0.3.0**.

## License

[MIT](LICENSE) © EmberStream Studio. All world, item, mob, and location names are original
EmberQuest IP.
