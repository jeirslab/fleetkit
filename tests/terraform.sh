#!/usr/bin/env bash
# Renders lib.internal.guests (what mkPulumi compiles) for the estates of tests/fixtures/tf-mini and checks
# each result against the pinned bpg/proxmox schema (tests/terraform.py), and
# that an estate whose guests are on two sites does not render. The estate
# "gaps" uses every option added for adopting existing guests (idmap, no
# console block, no pool, pool comment, scsi hardware, EFI disk, cloud-init
# drive slot and upgrade, clone and description under ignore_changes); the
# estate "quiet" uses adoption.unrecorded (what an import does not record,
# under ignore_changes or not; fleet.report.unrecordedIgnored); each
# case of tests/cases-guests.json is that fixture plus one bad declaration,
# which must fail evaluation with the message of the validation that refuses
# it ("expect", an extended regular expression). Evaluation only. Prints one
# JSON line: {"terraform":"pass"|"fail"}; exit 0 iff pass.
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

render() { # estate [module] -> $TMP/<estate>.json, or the error in $TMP/<estate>.err
  nix eval --impure --json "$ROOT#lib" --apply "l: l.internal.guests {
    fleet = l.fleet { modules = [ $ROOT/tests/fixtures/tf-mini ${2:-} ]; };
    estate = \"$1\";
  }" >"$TMP/$1.json" 2>"$TMP/$1.err"
}

status=pass
for estate in mini tenant bare gaps quiet; do
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

# Negative cases: the fixture plus one bad declaration each.
CASES="$ROOT/tests/cases-guests.json"
python3 - "$CASES" "$TMP" <<'PY' || status=fail
import json, sys
for c in json.load(open(sys.argv[1], encoding="utf-8"))["negative"]:
    open(f"{sys.argv[2]}/neg-{c['name']}.nix", "w", encoding="utf-8").write("\n".join(c["module"]) + "\n")
PY
negatives=0
while IFS=$'\t' read -r name expect; do
  negatives=$((negatives + 1))
  if render gaps "$TMP/neg-$name.nix"; then
    echo "FAIL negative $name: evaluated, but must be refused" >&2
    status=fail
  elif ! grep -Eq -- "$expect" "$TMP/gaps.err"; then
    echo "FAIL negative $name: the error does not mention /$expect/" >&2
    tail -n 8 "$TMP/gaps.err" >&2
    status=fail
  fi
done < <(python3 -c '
import json, sys
for c in json.load(open(sys.argv[1], encoding="utf-8"))["negative"]:
    print(c["name"] + "\t" + c["expect"])
' "$CASES")
if [[ $negatives -eq 0 ]]; then
  echo "FAIL: no negative case was read from $CASES" >&2
  status=fail
fi

# Raw lxc.idmap lines in lxcExtraConf are not rendered as idmap; the model
# reports them (fleet.report.lxcExtraConfIdmap), other raw lines it does not.
printf '%s\n' '_: { fleet.guests.gaps.conf.lxcExtraConf = [ "lxc.idmap: u 0 100000 65536" ]; }' >"$TMP/rawidmap.nix"
if ! out=$(nix eval --impure --json "$ROOT#lib" --apply "l: (l.fleet {
    modules = [ $ROOT/tests/fixtures/tf-mini $TMP/rawidmap.nix ];
  }).report.lxcExtraConfIdmap" 2>"$TMP/rawidmap.err") || [[ $out != '{"gaps/conf":["lxc.idmap: u 0 100000 65536"]}' ]]; then
  echo "FAIL report.lxcExtraConfIdmap: ${out:-$(tail -n 5 "$TMP/rawidmap.err")}" >&2
  status=fail
fi

# The guests whose unrecorded arguments are ignored, and which arguments: the
# three that take the estate's adoption.unrecorded = "ignore", not the two
# that set it back to "apply", and no guest of an estate that sets nothing.
want='{"quiet/ct":["cpu","memory","vm_id","console"],"quiet/machine":["cpu","memory","scsi_hardware","agent","operating_system","efi_disk"],"quiet/mixed":["cpu","memory","vm_id","console"]}'
if ! out=$(nix eval --impure --json "$ROOT#lib" --apply "l: (l.fleet {
    modules = [ $ROOT/tests/fixtures/tf-mini ];
  }).report.unrecordedIgnored" 2>"$TMP/unrecorded.err") || [[ $out != "$want" ]]; then
  echo "FAIL report.unrecordedIgnored: ${out:-$(tail -n 5 "$TMP/unrecorded.err")}" >&2
  status=fail
fi
# A guest's own "ignore" where the estate says nothing, and a guest's "apply"
# over the estate's "ignore", as the report sees them.
printf '%s\n' '_: { fleet.guests.gaps.conf.adoption.unrecorded = "ignore"; fleet.guests.quiet.ct.adoption.unrecorded = "apply"; }' >"$TMP/layers.nix"
if ! out=$(nix eval --impure --json "$ROOT#lib" --apply "l: builtins.attrNames (l.fleet {
    modules = [ $ROOT/tests/fixtures/tf-mini $TMP/layers.nix ];
  }).report.unrecordedIgnored" 2>"$TMP/layers.err") || [[ $out != '["gaps/conf","quiet/machine","quiet/mixed"]' ]]; then
  echo "FAIL report.unrecordedIgnored (layers): ${out:-$(tail -n 5 "$TMP/layers.err")}" >&2
  status=fail
fi

printf '{"terraform":"%s"}\n' "$status"
[[ $status == pass ]]
