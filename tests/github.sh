#!/usr/bin/env bash
# Renders lib.mkGithubTerraform for the estate "gh" of tests/fixtures/gh-mini
# and checks the result against the pinned integrations/github schema
# (tests/github.py). Evaluation only. Prints one JSON line:
# {"github":"pass"|"fail"}; exit 0 iff pass.
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

status=pass
if nix eval --impure --json "$ROOT#lib" --apply "l: l.mkGithubTerraform {
  fleet = l.fleet { modules = [ $ROOT/tests/fixtures/gh-mini ]; };
  estate = \"gh\";
}" >"$TMP/gh.json" 2>"$TMP/gh.err"; then
  python3 "$ROOT/tests/github.py" "$TMP/gh.json" >&2 || status=fail
else
  tail -n 40 "$TMP/gh.err" >&2
  status=fail
fi

printf '{"github":"%s"}\n' "$status"
[[ $status == pass ]]
