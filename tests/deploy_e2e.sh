#!/usr/bin/env bash
# Experimental. The deploy runner end to end on a throwaway estate repo: a git
# repo made here from this checkout, whose pulumi.nix imports the model's
# stack (fromModel, tf-mini's estate "mini") and adds a hand-written one
# composed from it, and whose hives.example is hive-mini through mkHive.
#
#   1. `fleetkit estates`; `fleetkit preview mini --hive example`: real pulumi
#      previews of both stacks, colmena called as `build -f <hive.nix>` (a
#      stand-in: building NixOS systems is out of scope here); the project dir
#      is Pulumi.yaml -> the program in the store;
#   2. backends: mini-guests keeps state in postgres when E2E_PG_URL is set,
#      mini-extra in S3 when E2E_S3_ENDPOINT/_KEY/_SECRET are set (both read
#      from a sops file, as the model's backends are), local otherwise; the
#      postgres URL never appears in the events;
#   3. GitOps: `fleetkit serve --repo` on the estate repo, push mode preview:
#      POST /v1/gitops/sync plans the head; a new commit pushed through the
#      signed webhook plans that commit; a bad signature is 401;
#   4. real colmena evaluates the runner's hive file.
#
# Not a gate: it needs the network (Pulumi plugins, provider binaries). No host
# is contacted. Prints {"deploy_e2e":"pass"|"fail"}.
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TMP="$(mktemp -d)"
SERVER=""
cleanup() { [[ -n $SERVER ]] && kill "$SERVER" 2>/dev/null; rm -rf "$TMP"; }
trap cleanup EXIT
status=pass
fail() { echo "FAIL: $*" >&2; status=fail; }

FK="$(nix build --no-link --print-out-paths "$ROOT#fleetkit")/bin/fleetkit" || { echo '{"deploy_e2e":"fail"}'; exit 1; }
tools() { nix shell nixpkgs#sops nixpkgs#age nixpkgs#jq nixpkgs#curl nixpkgs#openssl --command "$@"; }

# Backends: postgres and S3 when given, local otherwise.
PG="${E2E_PG_URL:-}"
S3="${E2E_S3_ENDPOINT:-}"
if [[ -n $PG ]]; then
  guests_backend='{ type = "postgres"; urlSecret = { path = "secrets/state.json"; extract = [ "pg" ]; }; }'
else
  guests_backend='{ type = "local"; path = "guests"; }'
fi
if [[ -n $S3 ]]; then
  extra_backend="{ type = \"s3\"; url = \"s3://${E2E_S3_BUCKET:-fleet-state}/e2e/?endpoint=$S3&region=garage&use_path_style=true\";
    env.AWS_ACCESS_KEY_ID = { path = \"secrets/state.json\"; extract = [ \"s3\" \"id\" ]; };
    env.AWS_SECRET_ACCESS_KEY = { path = \"secrets/state.json\"; extract = [ \"s3\" \"secret\" ]; }; }"
else
  extra_backend='{ type = "local"; path = "extra"; }'
fi

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
      pulumi = fleetkit.lib.pulumi.stacks {
        fleet = fleetkit.lib.fleet { modules = [ ./tf-mini ]; };
        modules = [ ./pulumi.nix ];
      };
      hives.example = fleetkit.lib.mkHive {
        fleet = fleetkit.lib.fleet { modules = [ ./hive-mini ]; };
        estate = "example";
        inherit nixpkgs;
      };
    };
}
EOF
cat >"$E/pulumi.nix" <<EOF
# The model's stack, and one more built from its parts.
{ fleetkit, config, ... }:
let guests = config.stacks.mini-guests; in
{
  imports = [ (fleetkit.lib.pulumi.fromModel { estate = "mini"; }) ];
  stacks.mini-guests.backend = $guests_backend;
  stacks.mini-extra = {
    estate = "mini";
    inherit (guests) packages variables;
    resources.provider-proxmox = guests.resources.provider-proxmox;
    resources.extra-pool = {
      type = "proxmox:index/virtualEnvironmentPool:VirtualEnvironmentPool";
      properties = { poolId = "extra"; comment = "hand-written in pulumi.nix"; };
      options.provider = "\\\${provider-proxmox}";
    };
    backend = $extra_backend;
  };
}
EOF
tools age-keygen -o "$TMP/age.key" 2>/dev/null
recipient="$(tools age-keygen -y "$TMP/age.key")"
printf '{"integrations":{"proxmox":{"main":{"api_token":"root@pam!e2e=00000000-0000-0000-0000-000000000000"}}}}' >"$E/secrets/tf.json"
tools jq -n --arg pg "$PG" --arg id "${E2E_S3_KEY:-}" --arg secret "${E2E_S3_SECRET:-}" \
  '{pg: $pg, s3: {id: $id, secret: $secret}}' >"$E/secrets/state.json"
for f in tf state; do tools sops --encrypt --age "$recipient" --in-place "$E/secrets/$f.json"; done
nix flake lock "$E" 2>/dev/null
git -C "$E" init -q -b main && git -C "$E" add -A \
  && git -C "$E" -c user.name=e2e -c user.email=e2e@example.com commit -qm one

cat >"$TMP/colmena" <<EOF
#!/usr/bin/env bash
echo "\$@" >>"$TMP/colmena.calls"
echo "stand-in colmena: \$*"
EOF
chmod +x "$TMP/colmena"

export FLEETKIT_STATE_DIR="$TMP/state" FLEETKIT_COLMENA="$TMP/colmena" PULUMI_CONFIG_PASSPHRASE="e2e"
export SOPS_AGE_KEY_FILE="$TMP/age.key" PULUMI_HOME="${PULUMI_HOME:-$TMP/pulumi-home}"
unset PULUMI_BACKEND_URL

# 1.
FLEETKIT_FLAKE="$E" "$FK" estates >"$TMP/estates.out" 2>&1 || fail "estates"
grep -qx 'mini: mini-extra mini-guests' "$TMP/estates.out" || { cat "$TMP/estates.out" >&2; fail "estates output"; }
if FLEETKIT_FLAKE="$E" "$FK" preview mini --hive example --json >"$TMP/preview.jsonl" 2>"$TMP/preview.err"; then
  sum() { tools jq -s --arg s "$1" 'map(select(.kind=="summary" and .stack==$s))[0].changes.create' "$TMP/preview.jsonl"; }
  [[ $(sum mini-guests) == 6 ]] || fail "mini-guests: $(sum mini-guests) creates, not 6"
  [[ $(sum mini-extra) == 3 ]] || fail "mini-extra: $(sum mini-extra) creates, not 3 (stack, provider, pool)"
  wd="$TMP/state/work/mini-guests"
  [[ $(readlink "$wd/Pulumi.yaml") == /nix/store/*-Pulumi.yaml ]] || fail "Pulumi.yaml is not the program in the store"
  [[ $(readlink "$wd/secrets/tf.json") == "$E/secrets/tf.json" ]] || fail "the secrets file is not linked"
  grep -q '00000000-0000-0000-0000-000000000000' "$TMP/preview.jsonl" && fail "token in the events"
  grep -q "^build -f $TMP/state/work/_hives/example.nix --impure$" "$TMP/colmena.calls" || fail "colmena build call"
else
  tail -n 40 "$TMP/preview.err" "$TMP/preview.jsonl" >&2
  fail "fleetkit preview"
fi

# 2.
backend() { tools jq -r -s --arg s "$1" 'map(select(.kind=="backend" and .stack==$s))[0].type' "$TMP/preview.jsonl"; }
if [[ -n $PG ]]; then
  [[ $(backend mini-guests) == postgres ]] || fail "mini-guests backend is $(backend mini-guests)"
  grep -qF "$PG" "$TMP/preview.jsonl" && fail "the postgres URL is in the events"
  nix shell nixpkgs#postgresql --command psql "$PG" -Atc "select key from pulumi_state" | grep -q '/stacks/mini-guests/main.json$' \
    || fail "no mini-guests stack in postgres"
else
  [[ -d $TMP/state/guests/.pulumi ]] || fail "no local state for mini-guests"
fi
if [[ -n $S3 ]]; then
  [[ $(backend mini-extra) == s3 ]] || fail "mini-extra backend is $(backend mini-extra)"
  AWS_ACCESS_KEY_ID="$E2E_S3_KEY" AWS_SECRET_ACCESS_KEY="$E2E_S3_SECRET" nix shell nixpkgs#awscli2 --command \
    aws --endpoint-url "$S3" --region garage s3 ls --recursive "s3://${E2E_S3_BUCKET:-fleet-state}/e2e/" \
    | grep -q 'e2e/.pulumi/stacks/mini-extra/main.json' || fail "no mini-extra stack in S3"
else
  [[ -d $TMP/state/extra/.pulumi ]] || fail "no local state for mini-extra"
fi

# 3. GitOps.
echo -n "e2e-token" >"$TMP/token"
WH="e2e-hook"
FLEETKIT_REPO="file://$E" FLEETKIT_DEPLOY_ON_PUSH=mini FLEETKIT_PUSH_MODE=preview FLEETKIT_WEBHOOK_SECRET="$WH" \
  FLEETKIT_STATE_DIR="$TMP/gstate" \
  "$FK" serve --listen 127.0.0.1:18741 --token-file "$TMP/token" >"$TMP/serve.log" 2>&1 &
SERVER=$!
API=http://127.0.0.1:18741
auth=(-H "Authorization: Bearer e2e-token" -H 'content-type: application/json')
for _ in $(seq 100); do tools curl -sf "$API/healthz" >/dev/null && break; sleep 0.2; done
wait_job() { # id -> final record
  local st
  for _ in $(seq 900); do
    st=$(tools curl -sf "${auth[@]}" "$API/v1/deploys/$1" | tools jq -r .state)
    [[ $st == running || $st == queued ]] || break
    sleep 1
  done
  tools curl -sf "${auth[@]}" "$API/v1/deploys/$1"
}
one=$(git -C "$E" rev-parse HEAD)
job=$(tools curl -sf "${auth[@]}" -X POST "$API/v1/gitops/sync" | tools jq -r .submitted.mini)
rec=$(wait_job "$job")
echo "$rec" | tools jq -e --arg r "$one" '.state == "succeeded" and .result.rev == $r and .request.preview' >/dev/null \
  || { echo "$rec" >&2; tail -n 30 "$TMP/serve.log" >&2; fail "gitops sync job"; }
echo "# two" >>"$E/pulumi.nix"
git -C "$E" -c user.name=e2e -c user.email=e2e@example.com commit -qam two
two=$(git -C "$E" rev-parse HEAD)
body="{\"ref\":\"refs/heads/main\",\"after\":\"$two\"}"
sig="sha256=$(printf '%s' "$body" | tools openssl dgst -sha256 -hmac "$WH" | awk '{print $NF}')"
code=$(tools curl -s -o /dev/null -w '%{http_code}' -X POST "$API/v1/hooks/github" -H 'x-github-event: push' \
  -H 'x-hub-signature-256: sha256=00' -d "$body")
[[ $code == 401 ]] || fail "badly signed webhook got $code"
job2=$(tools curl -sf -X POST "$API/v1/hooks/github" -H 'x-github-event: push' -H "x-hub-signature-256: $sig" \
  -H 'content-type: application/json' -d "$body" | tools jq -r .submitted.mini)
rec=$(wait_job "$job2")
echo "$rec" | tools jq -e --arg r "$two" '.state == "succeeded" and .result.rev == $r' >/dev/null \
  || { echo "$rec" >&2; fail "webhook job"; }
[[ -f $TMP/gstate/checkouts/$two/pulumi.nix ]] || fail "no checkout of $two"
tools curl -sf "${auth[@]}" "$API/v1/gitops" | tools jq -e --arg r "$two" '.submitted.mini.rev == $r' >/dev/null \
  || fail "gitops status"

# 4.
if nix shell nixpkgs#colmena --command colmena eval -f "$TMP/state/work/_hives/example.nix" --impure \
  -E '{ nodes, ... }: builtins.attrNames nodes' >"$TMP/colmena-eval.out" 2>"$TMP/colmena-eval.err"; then
  echo "colmena nodes: $(cat "$TMP/colmena-eval.out")" >&2
else
  tail -n 20 "$TMP/colmena-eval.err" >&2
  fail "colmena eval of the hive file"
fi

printf '{"deploy_e2e":"%s"}\n' "$status"
[[ $status == pass ]]
