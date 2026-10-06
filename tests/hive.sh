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

# Node case: mkSystems / mkHive with `site` return the node that names a
# nixos.module and nothing else; estate and site together, or neither, throw.
SITE_PRELUDE='
let
  kit = builtins.getFlake (toString ./.);
  inherit (kit.inputs) nixpkgs;
  fleet = kit.lib.fleet { modules = [ ./tests/fixtures/hive-mini ]; };
  site = "site1";
  hive = kit.lib.mkHive { inherit fleet site nixpkgs; network = "lan"; };
  systems = kit.lib.mkSystems { inherit fleet site nixpkgs; };
  node = fleet.sites.${site}.nodes.box1;
  inherit (nixpkgs) lib;
in
'
SITE_EXPR="$SITE_PRELUDE"'
let
  check = ok: msg: if ok then [ ] else [ msg ];
  sys = systems.box1;
  hiveNames = builtins.attrNames (removeAttrs hive [ "meta" ]);
  d = hive.box1.deployment;
  files = map (x: toString x.file) (sys.options.boot.isContainer.definitionsWithLocations ++ sys.options.fileSystems.definitionsWithLocations);
in
check (builtins.attrNames systems == [ "box1" ]) "mkSystems site nodes ${builtins.toJSON (builtins.attrNames systems)} are not [box1]"
++ check (sys.config.networking.hostName == "box1") "node hostName is ${sys.config.networking.hostName}"
++ check (!sys.config.boot.isContainer) "node has boot.isContainer set"
++ check (!(lib.any (m: lib.hasInfix "proxmox-lxc" m) files)) "node imports the proxmox-lxc profile"
++ check (hiveNames == [ "box1" ]) "mkHive site nodes ${builtins.toJSON hiveNames} are not [box1]"
++ check (d.targetHost == node.address) "node targetHost ${toString d.targetHost} is not ${node.address}"
++ check (builtins.elem "machine" d.tags) "node tags ${builtins.toJSON d.tags} lack machine"
'
site_out=$(nix eval --impure --json --expr "$SITE_EXPR" 2>"${TMPDIR:-/tmp}/hive-site.$$.err")
if [[ $? != 0 ]]; then
  fail=1
  log "FAIL: site eval"
  tail -n 40 "${TMPDIR:-/tmp}/hive-site.$$.err" >&2
else
  m=$(printf '%s' "$site_out" | python3 -c 'import json,sys; m=json.load(sys.stdin); print("\n".join(m)); sys.exit(1 if m else 0)') || {
    fail=1
    log "FAIL: $m"
  }
fi
rm -f "${TMPDIR:-/tmp}/hive-site.$$.err"

# Both estate and site, or neither, must throw (for mkSystems and mkHive).
for fn in mkSystems mkHive; do
  for args in 'estate = "example"; site = "site1";' ''; do
    extra=""
    [[ $fn == mkHive ]] && extra='network = "lan";'
    expr='
let
  kit = builtins.getFlake (toString ./.);
  inherit (kit.inputs) nixpkgs;
  fleet = kit.lib.fleet { modules = [ ./tests/fixtures/hive-mini ]; };
in
builtins.deepSeq (builtins.attrNames (kit.lib.'"$fn"' { inherit fleet nixpkgs; '"$extra $args"' })) true
'
    if nix eval --impure --json --expr "$expr" >/dev/null 2>&1; then
      fail=1
      log "FAIL: $fn with args [${args:-neither}] did not throw"
    fi
  done
done

if [[ $fail == 0 ]]; then
  log "ok"
  printf '{"hive":"pass"}\n'
else
  printf '{"hive":"fail"}\n'
  exit 1
fi
