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
#      signed webhook plans that commit; a bad signature is 401; a pull request
#      (refs/pull/1/head) is previewed at its head and reported to a stand-in
#      GitHub API: statuses and a comment with the real plan;
#      then the GitHub Action's client (actions/deploy) on the same server: a PR
#      preview with its summary and comment, a deploy of a commit not on the
#      branch refused, one on it run, a fork's PR skipped;
#      then a scoped token (FLEETKIT_API_TOKENS_FILE; its value in a file of
#      its own): a Colmena dry-activate of its hive at a commit on the branch
#      runs and the job says `by: tenant`; the Pulumi stage, another goal, the
#      estate's default hive, a commit off the branch and the GitOps routes are
#      each 403 with the field, and no job is made;
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
  # Every run has a directory of its own, removed at its end; `render` keeps its.
  [[ -z $(ls -A "$TMP/state/runs" 2>/dev/null) ]] || fail "the preview left its run directory"
  wd="$(FLEETKIT_FLAKE="$E" "$FK" render mini mini-guests | cut -d' ' -f1)"
  [[ $wd == "$TMP"/state/runs/*/mini-guests ]] || fail "render did not write to a run directory: $wd"
  [[ $(readlink "$wd/program") == /nix/store/*-Pulumi.yaml ]] || fail "program is not the program in the store"
  [[ -f $wd/Pulumi.yaml && ! -L $wd/Pulumi.yaml ]] || fail "Pulumi.yaml is not the run's own copy"
  [[ $(readlink "$wd/secrets/tf.json") == "$E/secrets/tf.json" ]] || fail "the secrets file is not linked"
  grep -q '00000000-0000-0000-0000-000000000000' "$TMP/preview.jsonl" && fail "token in the events"
  grep -Eq "^build -f $TMP/state/runs/[^/ ]+/_hives/example.nix --impure$" "$TMP/colmena.calls" || fail "colmena build call"
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
# A stand-in GitHub API: records statuses and comments as JSON lines.
cat >"$TMP/gh.py" <<'PYEOF'
import json, sys
from http.server import BaseHTTPRequestHandler, HTTPServer
log = open(sys.argv[2], "a")
class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def _reply(self, code, body):
        raw = json.dumps(body).encode(); self.send_response(code)
        self.send_header("content-length", str(len(raw))); self.end_headers(); self.wfile.write(raw)
    def do_GET(self): self._reply(200, [])
    def do_POST(self):
        b = json.loads(self.rfile.read(int(self.headers["content-length"])))
        log.write(json.dumps({"path": self.path, **b}) + "\n"); log.flush(); self._reply(201, {})
HTTPServer(("127.0.0.1", int(sys.argv[1])), H).serve_forever()
PYEOF
python3 "$TMP/gh.py" 18742 "$TMP/gh.jsonl" &
GH=$!
trap 'kill $GH 2>/dev/null; cleanup' EXIT
echo -n "e2e-token" >"$TMP/token"
# A scoped token: estate mini, the hive `example` only, Colmena dry-activate,
# no Pulumi stage, commits on the deploy branch. The tokens file names the
# file that holds the value.
echo "e2e-tenant" >"$TMP/tenant.token"
printf '{"tokens": {"tenant": {"file": "%s", "estates": ["mini"], "hives": ["example"], "nixos": "apply"}}}\n' \
  "$TMP/tenant.token" >"$TMP/tokens.json"
WH="e2e-hook"
FLEETKIT_REPO="file://$E" FLEETKIT_DEPLOY_ON_PUSH=mini FLEETKIT_PUSH_MODE=preview FLEETKIT_WEBHOOK_SECRET="$WH" \
  FLEETKIT_GITHUB_TOKEN=gh-e2e FLEETKIT_GITHUB_API=http://127.0.0.1:18742 \
  FLEETKIT_STATE_DIR="$TMP/gstate" FLEETKIT_API_TOKENS_FILE="$TMP/tokens.json" \
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

# A pull request: previewed at its head, reported back.
git -C "$E" checkout -q -b feature
echo "# three" >>"$E/pulumi.nix"
git -C "$E" -c user.name=e2e -c user.email=e2e@example.com commit -qam three
three=$(git -C "$E" rev-parse HEAD)
git -C "$E" update-ref refs/pull/1/head "$three"
git -C "$E" checkout -q main
body="{\"action\":\"opened\",\"repository\":{\"full_name\":\"example/estate\"},\"pull_request\":{\"number\":1,\"author_association\":\"OWNER\",\"base\":{\"ref\":\"main\"},\"head\":{\"sha\":\"$three\",\"repo\":{\"full_name\":\"example/estate\"}}}}"
sig="sha256=$(printf '%s' "$body" | tools openssl dgst -sha256 -hmac "$WH" | awk '{print $NF}')"
job3=$(tools curl -sf -X POST "$API/v1/hooks/github" -H 'x-github-event: pull_request' -H "x-hub-signature-256: $sig" \
  -H 'content-type: application/json' -d "$body" | tools jq -r .submitted.mini)
rec=$(wait_job "$job3")
echo "$rec" | tools jq -e --arg r "$three" '.state == "succeeded" and .result.rev == $r and .request.pr == 1' >/dev/null \
  || { echo "$rec" >&2; fail "pull request job"; }
sleep 1
tools jq -e -s --arg r "$three" 'map(select(.path == "/repos/example/estate/statuses/\($r)")) | map(.state) == ["pending", "success"]' \
  "$TMP/gh.jsonl" >/dev/null || { cat "$TMP/gh.jsonl" >&2; fail "pull request statuses"; }
tools jq -e -s 'map(select(.path == "/repos/example/estate/issues/1/comments"))[0].body
  | test("fleetkit:mini") and test("\\| `mini-guests` \\| 6 \\| 0 \\| 0 \\| 0 \\| 0 \\|") and test("\\| `mini-extra` \\| 3 ")' \
  "$TMP/gh.jsonl" >/dev/null || { cat "$TMP/gh.jsonl" >&2; fail "pull request comment"; }

# The GitHub Action's client (actions/deploy) against the same server.
act() { # out-prefix event-name event-json [VAR=value ...] -> exit status
  local out="$1" name="$2" evjson="$3"; shift 3
  printf '%s' "$evjson" >"$TMP/$out.event.json"
  env FLEETKIT_API_URL="$API" FLEETKIT_API_TOKEN=e2e-token FLEETKIT_ESTATES=mini FLEETKIT_MODE=auto \
    FLEETKIT_HIVE=example GITHUB_EVENT_NAME="$name" GITHUB_EVENT_PATH="$TMP/$out.event.json" \
    GITHUB_REPOSITORY=example/estate GITHUB_TOKEN=gh-e2e GITHUB_API_URL=http://127.0.0.1:18742 \
    GITHUB_STEP_SUMMARY="$TMP/$out.summary" GITHUB_OUTPUT="$TMP/$out.output" "$@" \
    python3 "$ROOT/actions/deploy/fleetkit_remote.py" >"$TMP/$out.log" 2>&1
}
pr2="{\"pull_request\":{\"number\":2,\"head\":{\"sha\":\"$three\",\"repo\":{\"full_name\":\"example/estate\"}}}}"
if act a pull_request "$pr2"; then
  grep -q '✅ fleetkit preview: `mini`' "$TMP/a.summary" || fail "action: preview summary"
  grep -q '"/repos/example/estate/issues/2/comments"' "$TMP/gh.jsonl" || fail "action: no PR comment"
else
  cat "$TMP/a.log" >&2; fail "action: PR preview"
fi
# A deploy of a commit that is not on the deploy branch is refused by the server.
if act b push '{}' FLEETKIT_INFRA=false GITHUB_SHA="$three"; then
  fail "action: deployed a commit that is not on main"
fi
grep -q 'is not on main' "$TMP/b.summary" || { cat "$TMP/b.log" >&2; fail "action: refusal not reported"; }
# A commit on main deploys (the Colmena stage only here, with the stand-in).
if act c push '{}' FLEETKIT_INFRA=false GITHUB_SHA="$two"; then
  grep -Eq "^apply switch -f $TMP/gstate/runs/[^/ ]+/_hives/example.nix --impure$" "$TMP/colmena.calls" \
    || fail "action: colmena apply not run"
else
  cat "$TMP/c.log" >&2; fail "action: deploy of a commit on main"
fi
# A fork's PR has no secrets: skipped, not failed.
fork="{\"pull_request\":{\"number\":3,\"head\":{\"sha\":\"$three\",\"repo\":{\"full_name\":\"someone/fork\"}}}}"
act d pull_request "$fork" FLEETKIT_API_TOKEN= || fail "action: fork PR failed instead of skipping"
grep -q '^skipped$' "$TMP/d.output" || fail "action: fork PR not reported skipped"

# A scoped token against the same server.
tenant=(-H "Authorization: Bearer e2e-tenant" -H 'content-type: application/json')
njobs() { tools curl -sf "${auth[@]}" "$API/v1/deploys?limit=1000" | tools jq length; }
scoped() { # request body -> the http code; the answer is in $TMP/scoped.json
  tools curl -s -o "$TMP/scoped.json" -w '%{http_code}' "${tenant[@]}" -X POST "$API/v1/deploys" -d "$1"
}
refused() { # what, field, request body
  local code
  code=$(scoped "$3")
  [[ $code == 403 && $(tools jq -r .detail.field "$TMP/scoped.json") == "$2" ]] \
    || { cat "$TMP/scoped.json" >&2; fail "scoped: $1 got $code, not 403 on $2"; }
}
before=$(njobs)
ok='"estate":"mini","hive":"example","infra":false,"goal":"dry-activate"'
refused "the Pulumi stage" infra "{\"estate\":\"mini\",\"hive\":\"example\",\"goal\":\"dry-activate\",\"rev\":\"$two\"}"
refused "a Pulumi preview" infra "{\"estate\":\"mini\",\"hive\":\"example\",\"preview\":true,\"rev\":\"$two\"}"
refused "the goal switch" goal "{\"estate\":\"mini\",\"hive\":\"example\",\"infra\":false,\"rev\":\"$two\"}"
refused "the estate's own hive" hive "{\"estate\":\"mini\",\"infra\":false,\"goal\":\"dry-activate\",\"rev\":\"$two\"}"
refused "another estate" estate '{"estate":"other","hive":"example","infra":false,"goal":"dry-activate"}'
refused "a commit that is not on main" rev "{$ok,\"rev\":\"$three\"}"
refused "a preview of a commit that is not on main" rev "{$ok,\"preview\":true,\"rev\":\"$three\"}"
refused "allow_delete" allow_delete "{$ok,\"rev\":\"$two\",\"allow_delete\":[\"web\"]}"
code=$(tools curl -s -o /dev/null -w '%{http_code}' "${tenant[@]}" -X POST "$API/v1/gitops/sync")
[[ $code == 403 ]] || fail "scoped: POST /v1/gitops/sync got $code, not 403"
code=$(tools curl -s -o /dev/null -w '%{http_code}' "${tenant[@]}" "$API/v1/gitops")
[[ $code == 403 ]] || fail "scoped: GET /v1/gitops got $code, not 403"
[[ $(njobs) == "$before" ]] || fail "scoped: a refused request made a job ($before jobs before, $(njobs) after)"
# What it may do: a dry-activate of a node of its hive, at a commit on main.
code=$(scoped "{$ok,\"on\":[\"web\"],\"rev\":\"$two\"}")
if [[ $code == 202 ]]; then
  sjob=$(tools jq -r .id "$TMP/scoped.json")
  rec=$(wait_job "$sjob")
  echo "$rec" | tools jq -e --arg r "$two" \
    '.state == "succeeded" and .by == "tenant" and .result.rev == $r and .request.rev == $r and .result.nixos == "dry-activate"' \
    >/dev/null || { echo "$rec" >&2; fail "scoped: the dry-activate job"; }
  grep -Eq "^apply dry-activate -f $TMP/gstate/runs/[^/ ]+/_hives/example.nix --impure --on web$" "$TMP/colmena.calls" \
    || fail "scoped: colmena dry-activate not run"
  # It reads its own job; the server's own jobs say who started them.
  [[ $(tools curl -s "${tenant[@]}" "$API/v1/deploys/$sjob" | tools jq -r .by) == tenant ]] || fail "scoped: reading its job"
  [[ $(tools curl -sf "${auth[@]}" "$API/v1/deploys/$job" | tools jq -r .by) == gitops ]] || fail "by of the sync job"
  [[ $(njobs) == $((before + 1)) ]] || fail "scoped: $(njobs) jobs, not $((before + 1))"
else
  cat "$TMP/scoped.json" >&2; fail "scoped: the dry-activate got $code"
fi
[[ $(tools curl -s "${tenant[@]}" "$API/v1/estates" | tools jq -c keys) == '["mini"]' ]] || fail "scoped: estates"
# Neither the value nor its digest is in the server's log, a job or an event.
digest=$(printf 'e2e-tenant' | sha256sum | cut -d' ' -f1)
if grep -rqs -e 'e2e-tenant' -e "$digest" "$TMP/serve.log" "$TMP/gstate/jobs"; then
  fail "scoped: the token is in a log or a job"
fi

# 4.
# The hive file the runner writes (into its run directory, gone by now).
echo "(builtins.getFlake (toString $E)).hives.example" >"$TMP/hive-example.nix"
if nix shell nixpkgs#colmena --command colmena eval -f "$TMP/hive-example.nix" --impure \
  -E '{ nodes, ... }: builtins.attrNames nodes' >"$TMP/colmena-eval.out" 2>"$TMP/colmena-eval.err"; then
  echo "colmena nodes: $(cat "$TMP/colmena-eval.out")" >&2
else
  tail -n 20 "$TMP/colmena-eval.err" >&2
  fail "colmena eval of the hive file"
fi

printf '{"deploy_e2e":"%s"}\n' "$status"
[[ $status == pass ]]
