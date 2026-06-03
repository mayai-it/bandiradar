#!/bin/sh
# Install the BandiRadar agent skill into Claude and Codex skill directories.
#
# By default installs into BOTH:
#   ~/.claude/skills/bandiradar
#   ${CODEX_HOME:-~/.codex}/skills/bandiradar
# Override with SKILLS_DIR=/some/dir to install into a single target instead.
#
# Usage:
#   ./install-skill.sh
#   SKILLS_DIR=~/.config/agent/skills ./install-skill.sh
set -e

SKILL_NAME="bandiradar"
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
SRC="$SCRIPT_DIR/skills/$SKILL_NAME"

if [ ! -d "$SRC" ]; then
    echo "Error: skill source not found at $SRC" >&2
    exit 1
fi

install_into() {
    target_root="$1"
    dest="$target_root/$SKILL_NAME"
    mkdir -p "$target_root"
    rm -rf "$dest"
    cp -R "$SRC" "$dest"
    echo "  installed -> $dest"
}

echo "Installing '$SKILL_NAME' skill from $SRC"

if [ -n "${SKILLS_DIR:-}" ]; then
    install_into "$SKILLS_DIR"
else
    install_into "$HOME/.claude/skills"
    install_into "${CODEX_HOME:-$HOME/.codex}/skills"
fi

echo "Done."
