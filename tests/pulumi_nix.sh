#!/usr/bin/env bash
# Experimental. Pulumi.nix (lib.pulumi) on tests/fixtures/pulumi-nix: a good
# stack evaluates, writes its program to the store, and the program is the
# typed, pruned properties; a misspelt property, a wrong type, a missing
# required one and a reference to nothing each fail `nix eval` with an error
# at the property's path. Adoption: a hand-written `adopt` is in adoptIds and
# not in the program; `adoptUnresolved` is listed while `adopt` is null;
# options.import fails evaluation (of the program and of adoptIds alone) with
# an error that names `adopt`. Evaluation only. Prints
# {"pulumi_nix":"pass"|"fail"}.
set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
status=pass

# The extra-pool resource of the fixture, as a module setting FIELD on it.
pool() { echo "{ stacks.mini-guests.resources.extra-pool.$1; }"; }
GOOD='{ poolId = "extra"; comment = "x"; }'
ev() { # props [module [what]] -> stdout json, or stderr in $TMP/err
  local what="${3:-{ inherit (s) file estate adoptIds adoptUnresolved; program = builtins.toJSON s.program;
         extra = s.program.resources.extra-pool.properties;
         box = s.program.resources.box.properties.vmId; \}}"
  nix eval --impure --json --expr "
    let
      kit = builtins.getFlake (toString $ROOT);
      stacks = kit.lib.pulumi.stacks {
        fleet = kit.lib.fleet { modules = [ $ROOT/tests/fixtures/tf-mini ]; };
        modules = [ (import $ROOT/tests/fixtures/pulumi-nix $1) ${2:-} ];
      };
      s = stacks.mini-guests;
    in $what" 2>"$TMP/err"
}
expect_fail() { # name props pattern [module [what]]
  if ev "$2" "${4:-}" "${5:-}" >/dev/null; then
    echo "FAIL $1: evaluated" >&2; status=fail
  elif ! grep -qE "$3" "$TMP/err"; then
    echo "FAIL $1: error is not /$3/" >&2; grep -E 'error:' "$TMP/err" | tail -2 >&2; status=fail
  fi
}

if out=$(ev "$GOOD"); then
  echo "$out" | python3 -c '
import json, sys
d = json.load(sys.stdin)
ok = d["extra"] == {"poolId": "extra", "comment": "x"} and d["box"] == 9001 and d["estate"] == "mini" \
  and d["file"].startswith("/nix/store/") and d["file"].endswith("-Pulumi.yaml")
sys.exit(0 if ok else 1)' || { echo "FAIL good: $out" >&2; status=fail; }
else
  tail -n 20 "$TMP/err" >&2; status=fail
fi
expect_fail typo '{ poolID = "extra"; }' "option .stacks\.mini-guests\.resources\.extra-pool\.properties\.poolID' does not exist"
expect_fail type '{ poolId = 7; }' "definition for option .stacks\.mini-guests\.resources\.extra-pool\.properties\.poolId' is not of type"
expect_fail missing '{ comment = "x"; }' "stacks\.mini-guests\.resources\.extra-pool\.properties: .*requires poolId"
expect_fail ref '{ poolId = "\${nope.id}"; }' "stacks\.mini-guests: references to no resource or variable: nope"

# Adoption ids are data beside the program.
adopt_case() { # name module python-condition-on-d
  local out
  if out=$(ev "$GOOD" "$2"); then
    echo "$out" | python3 -c "
import json, sys
d = json.load(sys.stdin)
sys.exit(0 if ($3) and '\"import\"' not in d['program'] and 'adopt' not in d['program'] else 1)" \
      || { echo "FAIL $1: $out" >&2; status=fail; }
  else
    echo "FAIL $1: evaluation" >&2; tail -n 20 "$TMP/err" >&2; status=fail
  fi
}
MODEL='"box": "n1/9001", "machine": "n1/9002", "tuned": "n1/9004", "main": "main"'
adopt_case model '' "d['adoptIds'] == {$MODEL} and d['adoptUnresolved'] == {}"
adopt_case hand "($(pool 'adopt = "extra"'))" \
  "d['adoptIds'] == {$MODEL, 'extra-pool': 'extra'} and d['adoptUnresolved'] == {}"
adopt_case unresolved "($(pool 'adoptUnresolved = "adopt = the pool id"'))" \
  "d['adoptIds'] == {$MODEL} and d['adoptUnresolved'] == {'extra-pool': 'adopt = the pool id'}"
adopt_case resolved "($(pool 'adopt = "extra"')) ($(pool 'adoptUnresolved = "adopt = the pool id"'))" \
  "d['adoptIds'] == {$MODEL, 'extra-pool': 'extra'} and d['adoptUnresolved'] == {}"
# An estate overrides a computed id with the module system.
adopt_case override '({ lib, ... }: { stacks.mini-guests.resources.box.adopt = lib.mkForce "n9/1"; })' \
  "d['adoptIds']['box'] == 'n9/1'"

# options.import is refused, by name, whatever is read.
IMPORT_ERR="stacks\.mini-guests\.resources\.extra-pool\.options\.import is not supported.*Set stacks\.mini-guests\.resources\.extra-pool\.adopt = "
expect_fail import "$GOOD" "$IMPORT_ERR" "($(pool 'options.import = "extra"'))"
expect_fail import-file "$GOOD" "$IMPORT_ERR" "($(pool 'options.import = "extra"'))" "s.file"
expect_fail import-ids "$GOOD" "$IMPORT_ERR" "($(pool 'options.import = "extra"'))" "s.adoptIds"

printf '{"pulumi_nix":"%s"}\n' "$status"
[[ $status == pass ]]
