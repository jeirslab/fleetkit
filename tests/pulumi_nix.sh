#!/usr/bin/env bash
# Experimental. Pulumi.nix (lib.pulumi) on tests/fixtures/pulumi-nix: a good
# stack evaluates, writes its program to the store, and the program is the
# typed, pruned properties; a misspelt property, a wrong type, a missing
# required one and a reference to nothing each fail `nix eval` with an error
# at the property's path. Evaluation only. Prints {"pulumi_nix":"pass"|"fail"}.
set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
status=pass

ev() { # props -> stdout json, or stderr in $TMP/err
  nix eval --impure --json --expr "
    let
      kit = builtins.getFlake (toString $ROOT);
      stacks = kit.lib.pulumi.stacks {
        fleet = kit.lib.fleet { modules = [ $ROOT/tests/fixtures/tf-mini ]; };
        modules = [ (import $ROOT/tests/fixtures/pulumi-nix $1) ];
      };
      s = stacks.mini-guests;
    in { inherit (s) file estate; extra = s.program.resources.extra-pool.properties;
         box = s.program.resources.box.properties.vmId; }" 2>"$TMP/err"
}
expect_fail() { # name props pattern
  if ev "$2" >/dev/null; then
    echo "FAIL $1: evaluated" >&2; status=fail
  elif ! grep -qE "$3" "$TMP/err"; then
    echo "FAIL $1: error is not /$3/" >&2; grep -E 'error:' "$TMP/err" | tail -2 >&2; status=fail
  fi
}

if out=$(ev '{ poolId = "extra"; comment = "x"; }'); then
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

printf '{"pulumi_nix":"%s"}\n' "$status"
[[ $status == pass ]]
