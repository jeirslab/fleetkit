#!/usr/bin/env bash
# Renders lib.mkGithubTerraform for the estate "gh" of tests/fixtures/gh-mini
# and checks the result against the pinned integrations/github schema
# (tests/github.py). Evaluation only. Three groups, from tests/cases-github.json:
#   base      the fixture alone (a Free-plan org, a private repo with an
#             environment, an organisation secret: both must be reported as
#             skipped, not rendered);
#   positive  the fixture plus a support module (a site, a guest) and every new
#             block (repo secret, variable, label, managed file, runner), also
#             with another estate's repository carrying the same blocks, which
#             must not reach estate gh's render;
#   negative  the fixture plus support plus one bad declaration, each of which
#             must fail evaluation with the message of the validation that
#             refuses it ("expect", an extended regular expression pinned to
#             that message, case-sensitive), and must never echo a literal
#             secret value. A case with a "tenant" block also writes that
#             module as <tmp>/tenant-<case>/fleet/default.nix and passes the
#             directory to lib.fleet as tenants.<name>, so the declaration is
#             judged as a tenant's (lib/default.nix, tenantViolations).
# Prints one JSON line: {"github":"pass"|"fail"}; exit 0 iff pass.
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CASES="$ROOT/tests/cases-github.json"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

status=pass
fail() {
  echo "github: FAIL $*" >&2
  status=fail
}

# Write each Nix module of the case file to $TMP/<name>.nix.
python3 - "$CASES" "$TMP" <<'PY' || exit 2
import json, os, sys
d = json.load(open(sys.argv[1], encoding="utf-8"))
out = sys.argv[2]
def w(name, lines):
    open(f"{out}/{name}.nix", "w", encoding="utf-8").write("\n".join(lines) + "\n")
w("support", d["support"])
w("positive", d["positive"])
w("isolation", d["isolation"]["module"])
for c in d["negative"]:
    w("neg-" + c["name"], c["module"])
    # A tenant source: <dir>/fleet/default.nix, the layout lib.mkFleet reads.
    if "tenant" in c:
        src = f"{out}/tenant-{c['name']}/fleet"
        os.makedirs(src)
        open(f"{src}/default.nix", "w", encoding="utf-8").write("\n".join(c["tenant"]["module"]) + "\n")
PY

# render NAME MODULE... : evaluate estate gh with the fixture plus the modules.
# TENANTS, when set, is the body of the tenants attrset (estate = source;).
render() {
  local name="$1" mods="" m
  shift
  for m in "$@"; do mods="$mods $TMP/$m.nix"; done
  nix eval --impure --json "$ROOT#lib" --apply "l: l.mkGithubTerraform {
    fleet = l.fleet { modules = [ $ROOT/tests/fixtures/gh-mini $mods ]; tenants = { ${TENANTS:-} }; };
    estate = \"gh\";
  }" >"$TMP/$name.json" 2>"$TMP/$name.err"
}

# base
if render base; then
  python3 "$ROOT/tests/github.py" "$TMP/base.json" >&2 || fail "base"
else
  tail -n 40 "$TMP/base.err" >&2
  fail "base: evaluation"
fi

# positive, with another estate's blocks present
if render positive support positive isolation; then
  python3 "$ROOT/tests/github.py" "$TMP/positive.json" --full --cases "$CASES" >&2 || fail "positive"
else
  tail -n 40 "$TMP/positive.err" >&2
  fail "positive: evaluation"
fi

# negative: each must fail to evaluate, name the offender, and not leak a value.
while IFS=$'\t' read -r name expect noecho tenant; do
  # A case with a tenant passes its source (written above) as that estate's
  # tenant, so the definition is judged by the tenant boundary.
  TENANTS=""
  [[ -n $tenant ]] && TENANTS="$tenant = \"$TMP/tenant-$name\";"
  if render "neg-$name" support "neg-$name"; then
    fail "negative $name: evaluated, but must be refused"
    continue
  fi
  if ! grep -Eq -- "$expect" "$TMP/neg-$name.err"; then
    fail "negative $name: the error does not mention /$expect/"
    tail -n 8 "$TMP/neg-$name.err" >&2
  fi
  # The module system quotes a definition it rejects as an unknown option, so
  # only the case that is refused by our own validation is held to this.
  if [[ $noecho == 1 ]] && grep -q 'LITERALVALUE' "$TMP/neg-$name.err"; then
    fail "negative $name: the error echoes the literal secret value"
  fi
done < <(python3 -c '
import json, sys
for c in json.load(open(sys.argv[1], encoding="utf-8"))["negative"]:
    print("\t".join([c["name"], c["expect"], "1" if c.get("noecho") else "0", c.get("tenant", {}).get("name", "")]))
' "$CASES")

printf '{"github":"%s"}\n' "$status"
[[ $status == pass ]]
