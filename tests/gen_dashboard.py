"""Build-time generator for dashboard_manifest.json.

Uses the yourbot_sdk.dashboard DSL (0.6.1) so widget types, widths, chart
types, and permissions are validated when this script runs — never at render
time. Run after any dashboard change, commit the JSON:

    .venv/bin/python tests/gen_dashboard.py

Lives under tests/ so it is excluded from the shipped artifact (the platform
zips and capability-scans every root-level .py; tests/ is exempt).
"""

import json
from pathlib import Path

from yourbot_sdk import dashboard as d

ROOT = Path(__file__).resolve().parent.parent

overview = d.Page("Overview", id="overview")
overview.add(d.StatCard("Registered players", rpc="get_player_count",
                        id="stat_players", refresh_seconds=60))
overview.add(d.StatCard("Commands today", rpc="get_commands_today",
                        id="stat_commands_today", refresh_seconds=60))
overview.add(d.StatCard("Embers in economy", rpc="get_economy_total",
                        id="stat_economy_total", refresh_seconds=60))
overview.add(d.StatCard("Dungeons cleared (30d)", rpc="get_dungeons_cleared",
                        id="stat_dungeons_cleared", refresh_seconds=300))
overview.add(d.StatCard("Duels fought (30d)", rpc="get_duels_fought",
                        id="stat_duels_fought", refresh_seconds=300))
overview.add(d.Chart("Commands — 7 days", rpc="get_commands_chart", id="chart_commands_7d",
                     chart_type="line", width="full", refresh_seconds=300))

settings = d.Page("Settings", id="settings", permission="manager")
form = d.Form(rpc="get_settings", save_rpc="save_settings",
              title="Economy settings", id="economy_settings")
form.number("Economy multiplier", key="economy_multiplier", min=0.1, max=10, step=0.1,
            help_text="Scales Ember rewards from hunts, adventures, dailies, dungeons, "
                      "and the arena prize bonus (0.1–10).")
form.channel_picker("Allowed channel", key="allowed_channel_id",
                    help_text="Restrict EmberQuest commands to one channel. Leave empty to allow all.")
settings.add(form)

manifest = d.Manifest(mode="manifest", pages=[overview, settings])

with open(ROOT / "dashboard_manifest.json", "w", encoding="utf-8") as fh:
    json.dump(manifest.to_json(), fh, indent=2)
    fh.write("\n")

print("dashboard_manifest.json written")
