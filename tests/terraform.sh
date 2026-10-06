#!/usr/bin/env bash
# Renders lib.mkTerraform for the estates of tests/fixtures/tf-mini and checks
# each result against the pinned bpg/proxmox schema (tests/terraform.py), and
# that an estate whose guests are on two sites does not render. Evaluation
# only. Prints one JSON line: {"terraform":"pass"|"fail"}; exit 0 iff pass.
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

render() { # estate -> $TMP/<estate>.json, or the error in $TMP/<estate>.err
  nix eval --impure --json "$ROOT#lib" --apply "l: l.mkTerraform {
    fleet = l.fleet { modules = [ $ROOT/tests/fixtures/tf-mini ]; };
    estate = \"$1\";
  }" >"$TMP/$1.json" 2>"$TMP/$1.err"
}

status=pass
for estate in mini tenant bare; do
  if render "$estate"; then
    python3 "$ROOT/tests/terraform.py" "$TMP/$estate.json" --estate "$estate" >&2 || status=fail
  else
    tail -n 40 "$TMP/$estate.err" >&2
    status=fail
  fi
done

# Guests on two sites and no placement: one provider cannot serve them; the
# message names both sites.
if render split; then
  echo "FAIL split: rendered guests on two sites" >&2
  status=fail
elif ! grep -Eq 'fleet\.estates\.split: no placement, and its guests and pools are on more than one site \((s1, s2|s2, s1)\)' "$TMP/split.err"; then
  echo "FAIL split: error is not the two-site message naming both sites" >&2
  tail -n 20 "$TMP/split.err" >&2
  status=fail
fi

# A placement on one site and a guest on another: still one provider, one site.
if render offsite; then
  echo "FAIL offsite: rendered a guest on another site than the placement" >&2
  status=fail
elif ! grep -q 'fleet\.estates\.offsite: guest far is on site "s2" but the provider is on site "s1"; one provider serves one site' "$TMP/offsite.err"; then
  echo "FAIL offsite: error is not the one-provider-one-site message" >&2
  tail -n 20 "$TMP/offsite.err" >&2
  status=fail
fi

printf '{"terraform":"%s"}\n' "$status"
[[ $status == pass ]]
