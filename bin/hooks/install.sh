#!/bin/sh
# One-time setup: use the tracked hooks in bin/hooks for this clone.
root=$(git rev-parse --show-toplevel) || exit 1
cd "$root" || exit 1
chmod +x bin/hooks/pre-commit
git config core.hooksPath bin/hooks
echo "Git hooks enabled (core.hooksPath = bin/hooks). Needs uv: https://docs.astral.sh/uv/"
