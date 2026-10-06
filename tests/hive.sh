#!/usr/bin/env bash
# mkHive / mkSystems check on the hive-mini fixture. Evaluation only (nix
# eval); nothing is built. Detail goes to stderr; stdout carries one JSON
# line, {"hive":"pass"|"fail"}. Exit 0 iff pass. Run by tools/gates.sh as
# part of the eval gate.
set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT" || exit 2

fail=0
log() { printf 'hive: %s\n' "$*" >&2; }

# The shared prelude: the checked fixture model and the kit's own nixpkgs.
# $NETWORK is the deploy network handed to mkHive.
PRELUDE='
let
  kit = builtins.getFlake (toString ./.);
  inherit (kit.inputs) nixpkgs;
  fleet = kit.lib.fleet { modules = [ ./tests/fixtures/hive-mini ]; };
  estate = "example";
  hive = kit.lib.mkHive { inherit fleet estate nixpkgs; network = NETWORK; };
  systems = kit.lib.mkSystems { inherit fleet estate nixpkgs; };
  guests = fleet.guests.${estate};
  names = builtins.sort builtins.lessThan (builtins.attrNames guests);
  inherit (nixpkgs) lib;
in
'

# Positive case: every check yields a message when it fails; [] = pass.
EXPR="${PRELUDE//NETWORK/\"lan\"}"'
let
  check = ok: msg: if ok then [ ] else [ msg ];
  hiveNames = builtins.sort builtins.lessThan (builtins.attrNames (removeAttrs hive [ "meta" ]));
  perGuest = n:
    let
      g = guests.${n};
      d = hive.${n}.deployment;
    in
    check (d.targetHost == g.ipv4.lan) "${n}: targetHost ${toString d.targetHost} is not ${g.ipv4.lan}"
    ++ check (d.targetUser == fleet.estates.${estate}.colmena.targetUser) "${n}: targetUser ${toString d.targetUser} is not the estates"
    ++ check (lib.all (t: builtins.elem t d.tags) (g.tags ++ [ g.kind estate ])) "${n}: tags ${builtins.toJSON d.tags} lack the guests tags, kind or estate";
in
check (hive ? meta) "mkHive has no meta"
++ check (hiveNames == names) "mkHive guests ${builtins.toJSON hiveNames} are not ${builtins.toJSON names}"
++ check (lib.sort builtins.lessThan (builtins.attrNames systems) == names) "mkSystems guests ${builtins.toJSON (builtins.attrNames systems)} are not ${builtins.toJSON names}"
++ check (systems.${builtins.head names}.config.networking.hostName == builtins.head names) "mkSystems hostName of ${builtins.head names} is ${systems.${builtins.head names}.config.networking.hostName}"
++ lib.concatMap perGuest names
'

out=$(nix eval --impure --json --expr "$EXPR" 2>"${TMPDIR:-/tmp}/hive-eval.$$.err")
rc=$?
if [[ $rc != 0 ]]; then
  fail=1
  log "FAIL: positive eval"
  tail -n 40 "${TMPDIR:-/tmp}/hive-eval.$$.err" >&2
else
  n=$(printf '%s' "$out" | python3 -c 'import json,sys; m=json.load(sys.stdin); print("\n".join(m)); sys.exit(1 if m else 0)') || {
    fail=1
    log "FAIL: $n"
  }
fi
rm -f "${TMPDIR:-/tmp}/hive-eval.$$.err"

# Negative case: a deploy network no guest has an address on must fail, naming
# a guest and the network.
NEG="${PRELUDE//NETWORK/\"nonet\"}"'
builtins.deepSeq (builtins.mapAttrs (_: v: v.deployment.targetHost) (removeAttrs hive [ "meta" ])) true
'
neg_err=$(nix eval --impure --json --expr "$NEG" 2>&1 >/dev/null)
if [[ $? == 0 ]]; then
  fail=1
  log "FAIL: mkHive with a network no guest has an address on did not throw"
elif ! grep -q 'nonet' <<<"$neg_err" || ! grep -Eq '\b(web|db)\b' <<<"$neg_err"; then
  fail=1
  log "FAIL: the error does not name a guest and the network"
  printf '%s\n' "$neg_err" | tail -n 15 >&2
fi

if [[ $fail == 0 ]]; then
  log "ok"
  printf '{"hive":"pass"}\n'
else
  printf '{"hive":"fail"}\n'
  exit 1
fi
