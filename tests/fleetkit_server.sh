#!/usr/bin/env bash
# nixosModules.fleetkit-server, evaluated (nix eval; nothing is built):
#   - every option of services.fleetkit, the options of a token included, has
#     a description;
#   - services.fleetkit.tokens renders to exactly cli/tests/tokens-module.json,
#     the file the server's own tests load (cli/tests/test_api.py), so the
#     module and the server cannot drift apart; the service is given it as
#     FLEETKIT_API_TOKENS_FILE, and is not when there are no tokens;
#   - a token's value cannot come through the store: a tokenFile that is a path
#     value, a store path or a relative path is refused, and so are a token
#     with both a tokenFile and a digest, with neither, and an unknown goal.
# Detail goes to stderr; stdout carries one JSON line,
# {"fleetkit_server":"pass"|"fail"}. Exit 0 iff pass. Run by tools/gates.sh as
# part of the eval gate.
set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT" || exit 2

fail=0
log() { printf 'fleetkit_server: %s\n' "$*" >&2; }
ERR="$(mktemp)"
trap 'rm -f "$ERR"' EXIT

# TOKENS is the value of services.fleetkit.tokens.
PRELUDE='
let
  kit = builtins.getFlake (toString ./.);
  inherit (kit.inputs) nixpkgs;
  inherit (nixpkgs) lib;
  system = with_: nixpkgs.lib.nixosSystem {
    system = "x86_64-linux";
    modules = [
      kit.nixosModules.fleetkit-server
      {
        services.fleetkit = {
          enable = true;
          repo = "git@example.com:example/estate.git";
          environmentFile = "/run/secrets/fleetkit.env";
          tokens = with_;
        };
      }
    ];
  };
  sys = system TOKENS;
  cfg = sys.config.services.fleetkit;
  # Assertions of this module only (not a whole system: no file systems, no boot loader).
  failed = builtins.filter (lib.hasPrefix "services.fleetkit") (
    map (a: a.message) (builtins.filter (a: !a.assertion) sys.config.assertions)
  );
in
'
GOOD='{
  tenant-ci = {
    tokenFile = "/run/secrets/fleetkit-token-tenant-ci";
    estates = [ "tenant" ];
    infra = "preview";
    nixos = "apply";
    goals = [ "dry-activate" "switch" ];
  };
  reader = {
    tokenFile = "/run/secrets/fleetkit-token-reader";
    estates = [ "tenant" ];
  };
  operator-ci = {
    sha256 = "5e884898da28047151d0e56f8dc6292773603d0d6aabbdd62a11ef721d1542d8";
    estates = [ "homelab" "tenant" ];
    hives = [ "homelab" "tenant" "nodes" ];
    infra = "apply";
    nixos = "apply";
    goals = [ "switch" "test" "boot" "dry-activate" ];
    revs = "any";
    allow = true;
  };
}'

EXPR="${PRELUDE//TOKENS/$GOOD}"'
let
  docs = lib.optionAttrSetToDocList sys.options.services.fleetkit;
  env = sys.config.systemd.services.fleetkit.environment;
in
{
  undescribed = map (d: d.name) (builtins.filter (d: (d.description or null) == null || d.description == "") docs);
  options = map (d: d.name) docs;
  rendered = builtins.fromJSON cfg.tokensFile.text;
  text = cfg.tokensFile.text;
  envIsFile = env.FLEETKIT_API_TOKENS_FILE == "${cfg.tokensFile}";
  inherit failed;
  noTokens = (system { }).config.systemd.services.fleetkit.environment ? FLEETKIT_API_TOKENS_FILE;
}
'
if out=$(nix eval --impure --json --expr "$EXPR" 2>"$ERR"); then
  msg=$(printf '%s' "$out" | python3 -c '
import json, sys
m = json.load(sys.stdin)
want = json.load(open("cli/tests/tokens-module.json"))
bad = []
if m["undescribed"]:
    bad.append("options without a description: " + ", ".join(m["undescribed"]))
for o in ("tokens", "tokens.<name>.tokenFile", "tokens.<name>.sha256", "tokens.<name>.estates", "tokens.<name>.hives",
          "tokens.<name>.infra", "tokens.<name>.nixos", "tokens.<name>.goals", "tokens.<name>.revs",
          "tokens.<name>.allow", "tokensFile"):
    if "services.fleetkit." + o not in m["options"]:
        bad.append("no option services.fleetkit." + o)
if m["rendered"] != want:
    bad.append("tokens render to %s, not to cli/tests/tokens-module.json" % json.dumps(m["rendered"], sort_keys=True))
if "/nix/store" in m["text"]:
    bad.append("the rendered tokens file names a store path")
if not m["envIsFile"]:
    bad.append("FLEETKIT_API_TOKENS_FILE is not the rendered file")
if m["failed"]:
    bad.append("assertions failed on a good config: " + "; ".join(m["failed"]))
if m["noTokens"]:
    bad.append("FLEETKIT_API_TOKENS_FILE is set with no tokens")
print("\n".join(bad))
sys.exit(1 if bad else 0)
') || {
    fail=1
    log "FAIL: $msg"
  }
else
  fail=1
  log "FAIL: the module does not evaluate"
  tail -n 40 "$ERR" >&2
fi

# Refused at evaluation: the value of each is services.fleetkit.tokens.
refused() { # what, tokens, a word the error must have
  local expr="${PRELUDE//TOKENS/$2}"'
if failed != [ ] then throw (builtins.concatStringsSep "; " failed) else builtins.fromJSON cfg.tokensFile.text
'
  if nix eval --impure --json --expr "$expr" >/dev/null 2>"$ERR"; then
    fail=1
    log "FAIL: $1 was not refused"
  elif ! rg -q -- "$3" "$ERR"; then
    fail=1
    log "FAIL: $1: the error does not say '$3'"
    tail -n 15 "$ERR" >&2
  fi
}
refused "a tokenFile that is a path value" '{ t = { tokenFile = ./flake.nix; estates = [ "e" ]; }; }' 'tokenFile'
refused "a tokenFile in the store" "{ t = { tokenFile = \"\${builtins.storeDir}/abc-token\"; estates = [ \"e\" ]; }; }" 'tokenFile'
refused "a relative tokenFile" '{ t = { tokenFile = "secrets/token"; estates = [ "e" ]; }; }' 'tokenFile'
refused "a token with a file and a digest" \
  '{ t = { tokenFile = "/run/secrets/t"; sha256 = "5e884898da28047151d0e56f8dc6292773603d0d6aabbdd62a11ef721d1542d8"; }; }' \
  'exactly one of tokenFile and sha256'
refused "a token with no value" '{ t = { estates = [ "e" ]; }; }' 'exactly one of tokenFile and sha256'
refused "a digest that is not one" '{ t = { sha256 = "abc"; }; }' 'sha256'
refused "an unknown goal" '{ t = { tokenFile = "/run/secrets/t"; goals = [ "yolo" ]; }; }' 'goals'
refused "an unknown infra" '{ t = { tokenFile = "/run/secrets/t"; infra = "yes"; }; }' 'infra'

if [[ $fail == 0 ]]; then
  log "ok"
  printf '{"fleetkit_server":"pass"}\n'
else
  printf '{"fleetkit_server":"fail"}\n'
  exit 1
fi
