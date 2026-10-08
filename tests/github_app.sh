#!/usr/bin/env bash
# Renders lib.mkGithubAppManifest for both tier selections and checks: only
# known manifest keys, only known permission names and levels, no webhook, and
# that terraform-admin (alone or with pipeline) is a superset of pipeline, and
# that permission keys exist in the pinned GitHub list (github/permission-names.txt).
# Evaluation only. Prints {"github_app":"pass"|"fail"}; exit 0 iff pass.
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

status=pass
render() { # NAME TIERS-NIX
  nix eval --json "$ROOT#lib" --apply "l: l.mkGithubAppManifest { org = \"acme\"; tiers = $2; redirectUrl = \"http://localhost:3000/cb\"; }" >"$TMP/$1.json" 2>"$TMP/$1.err" || {
    tail -n 20 "$TMP/$1.err" >&2
    return 1
  }
}
render both '[ "pipeline" "terraform-admin" ]' || status=fail
render pipeline '[ "pipeline" ]' || status=fail
render admin '[ "terraform-admin" ]' || status=fail
nix eval --json "$ROOT#lib" --apply 'l: (builtins.tryEval (l.mkGithubAppManifest { org = "acme"; tiers = [ "nope" ]; })).success' >"$TMP/bad.json" 2>/dev/null
[[ $(cat "$TMP/bad.json") == false ]] || { echo "github_app: unknown tier was accepted" >&2; status=fail; }

if [[ $status == pass ]]; then
  python3 - "$TMP" "$ROOT/github/permission-names.txt" <<'PY' >&2 || status=fail
import json, sys
tmp = sys.argv[1]
load = lambda n: json.load(open(f"{tmp}/{n}.json"))
KEYS = {"name", "url", "hook_attributes", "redirect_url", "description", "public",
        "default_events", "default_permissions", "callback_urls", "setup_url",
        "request_oauth_on_install", "setup_on_update"}
PERMS = {l.strip() for l in open(sys.argv[2]) if l.strip() and not l.startswith("#")}
if "variables" in PERMS or "actions_variables" not in PERMS: print("github_app: pinned permission list looks wrong"); sys.exit(1)
# Selftest: a key GitHub does not have must not pass the permission check.
if "invented_permission" in PERMS: print("github_app: selftest: invented key is in the pinned list"); sys.exit(1)
RANK = {"read": 1, "write": 2, "admin": 3}
bad = []
for n in ("both", "pipeline", "admin"):
    m = load(n)
    for k in m:
        if k not in KEYS: bad.append(f"{n}: unknown manifest key {k}")
    for k in ("name", "url"):
        if not m.get(k): bad.append(f"{n}: missing {k}")
    if m.get("public") is not False: bad.append(f"{n}: App must be private")
    if m["hook_attributes"].get("active") is not False: bad.append(f"{n}: webhook must be inactive")
    if m["default_events"] != []: bad.append(f"{n}: no events expected")
    if len(m["name"]) > 34: bad.append(f"{n}: name too long")
    for k, v in m["default_permissions"].items():
        if k not in PERMS: bad.append(f"{n}: unknown permission {k}")
        if v not in RANK: bad.append(f"{n}: bad level {k}={v}")
p, a, b = (load(x)["default_permissions"] for x in ("pipeline", "admin", "both"))
if load("both")["name"] != "acme-fleet": bad.append("default name is not <org>-fleet")
for k, v in p.items():
    if RANK[b.get(k, "read")] < RANK[v] or k not in b: bad.append(f"both lacks pipeline permission {k}={v}")
for k, v in a.items():
    if k not in b or RANK[b[k]] < RANK[v]: bad.append(f"both lacks terraform-admin permission {k}={v}")
if set(b) != set(p) | set(a): bad.append("both is not the union of the tiers")
for k, v in p.items():
    if k not in a or RANK[a[k]] < RANK[v]: bad.append(f"terraform-admin alone lacks pipeline permission {k}={v}")
if not (set(a) - set(p)): bad.append("terraform-admin adds nothing to pipeline")
for x in bad: print("github_app:", x)
sys.exit(1 if bad else 0)
PY
fi
printf '{"github_app":"%s"}\n' "$status"
[[ $status == pass ]]
