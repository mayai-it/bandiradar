# BandiRadar — self-healing crawl demo (offline, no network, no LLM key).
# Render once:  brew install vhs  &&  vhs docs/self-heal.tape   ->  docs/self-heal.gif
#
# The "money shot": a scraper's listing changes shape, and the crawl re-derives its
# own recipe and recovers — the agent proposes data, a deterministic guard adopts it
# only if it exactly reproduces the last-good results.

Output docs/self-heal.gif

Require uv

Set Shell bash
Set FontSize 16
Set Width 1180
Set Height 340
Set Padding 22
Set Theme "Catppuccin Mocha"
Set TypingSpeed 55ms

# --- setup (hidden): enter the repo, warm uv's env so the run is clean, clear ---
Hide
Type "cd /Users/danielegiovanardi/bandiradar"
Enter
Type "uv run python -c '1' >/dev/null 2>&1"
Enter
Type "clear"
Enter
Show

# --- the shot: the listing drifted; watch the crawl heal itself, offline ---
Type "# a scraper's listing changed shape — watch the crawl heal itself, offline"
Sleep 900ms
Enter
Type "uv run python scripts/demo_self_heal.py"
Sleep 600ms
Enter
Sleep 4500ms
Sleep 1s
