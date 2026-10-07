#!/usr/bin/env bash
# Experimental. The deploy runner end to end on a throwaway estate repo: a flake
# made here from this checkout, with pulumi.mini.guests (tf-mini through
# mkPulumi) and hives.example (hive-mini through mkHive).
#
#   1. `fleetkit estates` lists the estate and its stack;
#   2. `fleetkit preview mini --hive example`: a real pulumi preview (the guests
#      and pool planned as creates), then colmena build on the hive (a stand-in
#      colmena that records its arguments: building NixOS systems is out of
#      scope here);
#   3. the same preview through `fleetkit serve`: 401 without the token, 202,
#      the job's events, the summary, and a failed job for an unknown estate;
#   4. real colmena evaluates the hive file the runner writes (node names).
#
# Not a gate: it needs the network (Pulumi plugins, provider binaries). State is
# a file backend in a temp dir and the secret a throwaway age key; no host is
# contacted. Prints {"deploy_e2e":"pass"|"fail"}.
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TMP="$(mktemp -d)"
SERVER=""
cleanup() { [[ -n $SERVER ]] && kill "$SERVER" 2>/dev/null; rm -rf "$TMP"; }
trap cleanup EXIT
status=pass
fail() { echo "FAIL: $*" >&2; status=fail; }

FK="$(nix build --no-link --print-out-paths "$ROOT#fleetkit")/bin/fleetkit" || { echo '{"deploy_e2e":"fail"}'; exit 1; }
tools() { nix shell nixpkgs#sops nixpkgs#age nixpkgs#jq nixpkgs#curl --command "$@"; }

# The estate repo.
E="$TMP/estate"
mkdir -p "$E/secrets"
cp -r "$ROOT/tests/fixtures/tf-mini" "$E/tf-mini"
cp -r "$ROOT/tests/fixtures/hive-mini" "$E/hive-mini"
cat >"$E/flake.nix" <<EOF
{
  inputs.fleetkit.url = "path:$ROOT";
  outputs = { fleetkit, ... }:
    let inherit (fleetkit.inputs) nixpkgs; in {
      pulumi.mini.guests = fleetkit.lib.mkPulumi {
        fleet = fleetkit.lib.fleet { modules = [ ./tf-mini ]; };
        estate = "mini";
      };
      hives.example = fleetkit.lib.mkHive {
        fleet = fleetkit.lib.fleet { modules = [ ./hive-mini ]; };
        estate = "example";
        inherit nixpkgs;
      };
    };
}
EOF
tools age-keygen -o "$TMP/age.key" 2>/dev/null
printf '{"integrations":{"proxmox":{"main":{"api_token":"root@pam!e2e=00000000-0000-0000-0000-000000000000"}}}}' >"$E/secrets/tf.json"
tools sops --encrypt --age "$(tools age-keygen -y "$TMP/age.key")" --in-place "$E/secrets/tf.json"

# A colmena stand-in that records how it was called.
cat >"$TMP/colmena" <<EOF
#!/usr/bin/env bash
echo "\$@" >>"$TMP/colmena.calls"
echo "stand-in colmena: \$*"
EOF
chmod +x "$TMP/colmena"

export FLEETKIT_FLAKE="$E" FLEETKIT_STATE_DIR="$TMP/state" FLEETKIT_COLMENA="$TMP/colmena"
export PULUMI_BACKEND_URL="file://$TMP/pulumi-state" PULUMI_CONFIG_PASSPHRASE="e2e"
export SOPS_AGE_KEY_FILE="$TMP/age.key" PULUMI_HOME="${PULUMI_HOME:-$TMP/pulumi-home}"
mkdir -p "$TMP/pulumi-state"

# 0. The safety rails: no backend, no run.
if PULUMI_BACKEND_URL= "$FK" estates >/dev/null 2>"$TMP/nobackend.err"; then
  fail "ran without PULUMI_BACKEND_URL"
fi
grep -q PULUMI_BACKEND_URL "$TMP/nobackend.err" || fail "no-backend message"

# 1.
"$FK" estates >"$TMP/estates.out" 2>&1 || fail "estates"
grep -qx 'mini: guests' "$TMP/estates.out" || { cat "$TMP/estates.out" >&2; fail "estates output"; }

# 2.
if "$FK" preview mini --hive example --json >"$TMP/preview.jsonl" 2>"$TMP/preview.err"; then
  tools jq -e -s 'map(select(.kind=="summary"))[0].changes.create == 6' "$TMP/preview.jsonl" >/dev/null \
    || { grep summary "$TMP/preview.jsonl" >&2; fail "preview summary is not 6 creates (stack, provider, 3 guests, pool)"; }
  tools jq -e -s 'map(select(.kind=="step" and .op=="create")) | length >= 4' "$TMP/preview.jsonl" >/dev/null \
    || fail "preview steps"
  grep -q '00000000-0000-0000-0000-000000000000' "$TMP/preview.jsonl" && fail "token in the events"
  # The project dir: Pulumi.json, Main.json a rooted link to the program in
  # the store, and the secrets file linked from the estate repo.
  wd="$TMP/state/work/mini/guests"
  [[ -f $wd/Pulumi.json && ! -e $wd/Pulumi.yaml ]] || fail "the project file is not Pulumi.json"
  [[ $(readlink "$wd/Main.json") == /nix/store/*-Main.json ]] || fail "Main.json is not the program in the store"
  [[ $(readlink "$wd/secrets/tf.json") == "$E/secrets/tf.json" ]] || fail "the secrets file is not linked"
  grep -q "$(readlink "$wd/Main.json")" "$TMP/preview.jsonl" || fail "the program's store path is not in the events"
  grep -q "^build -f $TMP/state/work/_hives/example.nix --impure$" "$TMP/colmena.calls" \
    || { cat "$TMP/colmena.calls" >&2; fail "colmena build call"; }
else
  tail -n 40 "$TMP/preview.err" "$TMP/preview.jsonl" >&2
  fail "fleetkit preview"
fi

# 3.
echo -n "e2e-token" >"$TMP/token"
"$FK" serve --listen 127.0.0.1:18740 --token-file "$TMP/token" >"$TMP/serve.log" 2>&1 &
SERVER=$!
API=http://127.0.0.1:18740
for _ in $(seq 100); do tools curl -sf "$API/healthz" >/dev/null && break; sleep 0.2; done
code=$(tools curl -s -o /dev/null -w '%{http_code}' -X POST "$API/v1/deploys" -H 'content-type: application/json' -d '{"estate":"mini"}')
[[ $code == 401 ]] || fail "unauthenticated deploy got $code"
auth=(-H "Authorization: Bearer e2e-token" -H 'content-type: application/json')
job=$(tools curl -sf "${auth[@]}" -X POST "$API/v1/deploys" -d '{"estate":"mini","hive":"example","preview":true}' | tools jq -r .id)
bad=$(tools curl -sf "${auth[@]}" -X POST "$API/v1/deploys" -d '{"estate":"nope","nixos":false}' | tools jq -r .id)
state=""
for _ in $(seq 600); do
  state=$(tools curl -sf "${auth[@]}" "$API/v1/deploys/$job" | tools jq -r .state)
  [[ $state == running || $state == queued ]] || break
  sleep 1
done
[[ $state == succeeded ]] || { tools curl -sf "${auth[@]}" "$API/v1/deploys/$job/events" >&2; fail "API job is $state"; }
tools curl -sf "${auth[@]}" "$API/v1/deploys/$job" | tools jq -e '.result.infra.guests.create == 6 and .result.nixos == "built" and (.result.programs.guests | startswith("/nix/store/"))' >/dev/null \
  || fail "API job result"
tools curl -sf "${auth[@]}" "$API/v1/deploys/$job/stream" | grep -q '^event: end' || fail "API stream end"
bstate=$(tools curl -sf "${auth[@]}" "$API/v1/deploys/$bad" | tools jq -r '.state + " " + .error')
[[ $bstate == "failed RenderError: no estate 'nope'"* ]] || fail "unknown estate job: $bstate"

# 4. Real colmena reads the runner's hive file.
if nix shell nixpkgs#colmena --command colmena eval -f "$TMP/state/work/_hives/example.nix" --impure \
  -E '{ nodes, ... }: builtins.attrNames nodes' >"$TMP/colmena-eval.out" 2>"$TMP/colmena-eval.err"; then
  echo "colmena nodes: $(cat "$TMP/colmena-eval.out")" >&2
else
  tail -n 20 "$TMP/colmena-eval.err" >&2
  fail "colmena eval of the hive file"
fi

printf '{"deploy_e2e":"%s"}\n' "$status"
[[ $status == pass ]]
