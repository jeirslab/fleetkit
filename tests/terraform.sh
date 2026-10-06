#!/usr/bin/env bash
# Renders lib.mkTerraform on tests/fixtures/tf-mini and checks the result
# against the pinned bpg/proxmox schema (tests/terraform.py). Evaluation
# only. Prints one JSON line: {"terraform":"pass"|"fail"}; exit 0 iff pass.
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

status=fail
if nix eval --impure --json "$ROOT#lib" --apply "l: l.mkTerraform {
  fleet = l.fleet { modules = [ $ROOT/tests/fixtures/tf-mini ]; };
  estate = \"mini\";
}" >"$TMP/main.tf.json" 2>"$TMP/eval.err"; then
  python3 "$ROOT/tests/terraform.py" "$TMP/main.tf.json" >&2 && status=pass
else
  tail -n 40 "$TMP/eval.err" >&2
fi

printf '{"terraform":"%s"}\n' "$status"
[[ $status == pass ]]
