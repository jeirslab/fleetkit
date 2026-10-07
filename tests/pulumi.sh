#!/usr/bin/env bash
# Experimental. Renders lib.mkPulumi for the estates of tests/fixtures/tf-mini
# and lib.mkGithubPulumi for gh-mini, and checks each against its Terraform
# render and the pinned Pulumi schemas (tests/pulumi.py); that the estates
# the internal stage refuses (split, offsite) are refused by mkPulumi with a
# message naming it; and that the name maps are what tests/gen_pulumi_names.py makes from
# the pinned schemas. Evaluation only. Prints one JSON line:
# {"pulumi":"pass"|"fail"}; exit 0 iff pass.
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

render() { # fn fixture estate -> $TMP/<estate>.<fn>.json, or the error in .err
  nix eval --impure --json "$ROOT#lib" --apply "l: l.${1//_/.} {
    fleet = l.fleet { modules = [ $ROOT/tests/fixtures/$2 ]; };
    estate = \"$3\";
  }" >"$TMP/$3.$1.json" 2>"$TMP/$3.$1.err"
}

status=pass
pair() { # tfFn pulumiFn fixture estate
  if render "$1" "$3" "$4" && render "$2" "$3" "$4"; then
    python3 "$ROOT/tests/pulumi.py" "$TMP/$4.$2.json" "$TMP/$4.$1.json" || status=fail
  else
    tail -n 40 "$TMP/$4.$1.err" "$TMP/$4.$2.err" >&2
    status=fail
  fi
}
for estate in mini tenant bare; do
  pair internal_guests mkPulumi tf-mini "$estate"
done
pair internal_github mkGithubPulumi gh-mini gh

# What the internal stage refuses, mkPulumi refuses, naming mkPulumi.
for estate in split offsite; do
  if render mkPulumi tf-mini "$estate"; then
    echo "FAIL $estate: mkPulumi rendered what the internal stage refuses" >&2
    status=fail
  elif ! grep -q "mkPulumi: fleet\.estates\.$estate: " "$TMP/$estate.mkPulumi.err"; then
    echo "FAIL $estate: mkPulumi's error does not name it" >&2
    tail -n 20 "$TMP/$estate.mkPulumi.err" >&2
    status=fail
  fi
done

# The name maps are regenerated from the pinned schemas, not hand-edited.
cp -r "$ROOT/providers/pulumi/names" "$TMP/names.before"
if ! python3 "$ROOT/tests/gen_pulumi_names.py" 2>"$TMP/gen.err"; then
  cat "$TMP/gen.err" >&2
  status=fail
elif ! diff -r "$TMP/names.before" "$ROOT/providers/pulumi/names" >&2; then
  echo "FAIL: providers/pulumi/names differ from tests/gen_pulumi_names.py output" >&2
  status=fail
fi

printf '{"pulumi":"%s"}\n' "$status"
[[ $status == pass ]]
