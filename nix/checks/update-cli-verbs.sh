#!/usr/bin/env bash
# Regenerate the committed CLI verb-surface golden from the current tree.
#
# Run after an INTENTIONAL change to the `fleet` command tree (a new/renamed/
# removed verb, or a component family folding its CLI upward), review the diff,
# and `git add` the result. The cli-verbs-golden flake check fails until the
# committed golden matches `fleet --dump-verbs`.
set -euo pipefail

root="$(git -C "$(dirname "$0")" rev-parse --show-toplevel)"
cd "$root"

# Build the framework CLI (no trogon / consumer extensions, same as the check)
# and dump its verb surface.
nix run .#fleet -- --dump-verbs | jq -S . > nix/checks/golden/cli-verbs.json
echo "wrote nix/checks/golden/cli-verbs.json — review the diff, then: git add nix/checks/golden/cli-verbs.json"
