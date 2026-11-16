#!/usr/bin/env bash
# The Google add-on (addons.google, modules/addons.nix, lib/google.nix) on the
# estates of tests/fixtures/google-mini. Checks that:
#   - the estate "gcp" renders one google_project_service per declared service
#     (key <project>-<service without .googleapis.com>), disable_on_destroy
#     rendered on each (false unless the project says true), the provider's
#     credentials a data.sops_file reference and never a literal, and the
#     google and sops providers at their pins; and its Pulumi program is the
#     same, checked against the pinned schema (tests/pulumi.py);
#   - the stack <estate>-google exists for "gcp" only: fromModel { google = true; }
#     on "plain" (the add-on off) adds no stack, and mkGooglePulumi refuses it;
#   - no estate of tests/fixtures/tf-mini or gh-mini renders anything of Google
#     (checked in tests/addons.sh, for the guests);
#   - each case of tests/cases-google.json (the fixture plus one bad
#     declaration) is refused with its message ("expect", an extended regular
#     expression), and no message echoes a key pasted where a ref goes.
# Evaluation only. Prints one JSON line: {"google":"pass"|"fail"}; exit 0 iff pass.
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
FIX="$ROOT/tests/fixtures/google-mini"

status=pass
fail() {
  echo "FAIL $*" >&2
  status=fail
}

render() { # fn estate -> $TMP/<estate>.<fn>.json
  nix eval --impure --json "$ROOT#lib" --apply "l: l.${1//_/.} {
    fleet = l.fleet { modules = [ $FIX ]; };
    estate = \"$2\";
  }" >"$TMP/$2.$1.json" 2>"$TMP/$2.$1.err"
}

if render internal_google gcp && render mkGooglePulumi gcp; then
  python3 "$ROOT/tests/pulumi.py" "$TMP/gcp.mkGooglePulumi.json" "$TMP/gcp.internal_google.json" || status=fail
  python3 - "$TMP/gcp.internal_google.json" "$TMP/gcp.mkGooglePulumi.json" \
    "$ROOT/providers/schemas/hashicorp-google-8.10.0.schema.json" <<'PY' >&2 || status=fail
import json, sys
tf, prog = (json.load(open(f)) for f in sys.argv[1:3])
bad = []
want = {
    "main-drive": ("example-project-123456", "drive.googleapis.com", False),
    "main-calendar-json": ("example-project-123456", "calendar-json.googleapis.com", False),
    "main-serviceusage": ("example-project-123456", "serviceusage.googleapis.com", False),
    "side-people": ("example-side-654321", "people.googleapis.com", True),
}
got = tf["resource"]["google_project_service"]
if set(got) != set(want):
    bad.append(f"resources are {sorted(got)}, expected {sorted(want)}")
for k, (project, service, disable) in want.items():
    if got.get(k) != {"project": project, "service": service, "disable_on_destroy": disable}:
        bad.append(f"{k}: {got.get(k)}")
    p = (prog["resources"].get(k) or {}).get("properties")
    if p != {"project": project, "service": service, "disableOnDestroy": disable}:
        bad.append(f"pulumi {k}: {p}")
    if (prog["resources"].get(k) or {}).get("type") != "google:index/projectService:ProjectService":
        bad.append(f"pulumi {k}: type {(prog['resources'].get(k) or {}).get('type')}")
    if (prog["resources"].get(k) or {}).get("options") != {"provider": "${provider-google}"}:
        bad.append(f"pulumi {k}: options {(prog['resources'].get(k) or {}).get('options')}")
rp = tf["terraform"]["required_providers"]
if rp != {"google": {"source": "hashicorp/google", "version": "8.10.0"},
          "sops": {"source": "carlpett/sops", "version": "1.4.1"}}:
    bad.append(f"required_providers: {rp}")
if tf["provider"] != {"google": {"credentials": '${data.sops_file.hosts.data["integrations.google.main.deployer_key"]}'}}:
    bad.append(f"provider: {tf['provider']}")
if tf["data"] != {"sops_file": {"hosts": {"source_file": "secrets/hosts.yaml"}}}:
    bad.append(f"data: {tf['data']}")
if prog["name"] != "gcp-google":
    bad.append(f"project name {prog['name']}")
pk = prog["packages"].get("google")
if pk != {"source": "terraform-provider", "version": "1.4.0", "parameters": ["hashicorp/google", "8.10.0"]}:
    bad.append(f"packages.google: {pk}")
# The render against the pinned Terraform schema: every argument exists and
# the required ones are there.
sch = next(iter(json.load(open(sys.argv[3])).values()))
attrs = sch["resource_schemas"]["google_project_service"]["block"]["attributes"]
for k, body in got.items():
    for a in body:
        if a not in attrs:
            bad.append(f"{k}.{a}: not in the pinned hashicorp/google schema")
    for a, d in attrs.items():
        if d.get("required") and a not in body:
            bad.append(f"{k}.{a}: required, not rendered")
for a in tf["provider"]["google"]:
    if a not in sch["provider"]["block"]["attributes"]:
        bad.append(f"provider.google.{a}: not in the pinned schema")
for b in bad:
    print(f"google: {b}", file=sys.stderr)
sys.exit(1 if bad else 0)
PY
else
  tail -n 40 "$TMP"/gcp.*.err >&2
  fail "the estate gcp does not render"
fi

# The stack: gcp has one, plain none; mkGooglePulumi refuses plain.
if ! out=$(nix eval --impure --json --expr "
    let
      kit = builtins.getFlake (toString $ROOT);
      s = kit.lib.pulumi.stacks {
        fleet = kit.lib.fleet { modules = [ $FIX ]; };
        modules = map (estate: kit.lib.pulumi.fromModel { inherit estate; google = true; }) [ \"gcp\" \"plain\" ];
      };
    in { names = builtins.attrNames s; estate = s.gcp-google.estate; file = builtins.isString s.gcp-google.file; }" 2>"$TMP/stack.err"); then
  tail -n 20 "$TMP/stack.err" >&2
  fail "the stacks of the fixture do not evaluate"
elif [[ $out != '{"estate":"gcp","file":true,"names":["gcp-google"]}' ]]; then
  fail "stacks: $out"
fi
if render mkGooglePulumi plain; then
  fail "plain: mkGooglePulumi rendered an estate that does not switch the add-on on"
elif ! grep -q "addons\.google\.enable is false" "$TMP/plain.mkGooglePulumi.err"; then
  fail "plain: the refusal does not say the add-on is off"
fi

# Negative cases: the fixture plus one bad declaration each.
CASES="$ROOT/tests/cases-google.json"
python3 - "$CASES" "$TMP" <<'PY' || status=fail
import json, sys
for c in json.load(open(sys.argv[1], encoding="utf-8"))["negative"]:
    open(f"{sys.argv[2]}/neg-{c['name']}.nix", "w", encoding="utf-8").write("\n".join(c["module"]) + "\n")
PY
negatives=0
while IFS=$'\t' read -r name expect; do
  negatives=$((negatives + 1))
  if nix eval --impure --json "$ROOT#lib" --apply "l: l.mkGooglePulumi {
      fleet = l.fleet { modules = [ $FIX $TMP/neg-$name.nix ]; };
      estate = \"gcp\";
    }" >/dev/null 2>"$TMP/neg.err"; then
    fail "negative $name: evaluated, but must be refused"
  elif ! grep -Eq -- "$expect" "$TMP/neg.err"; then
    fail "negative $name: the error does not mention /$expect/"
    tail -n 8 "$TMP/neg.err" >&2
  elif grep -q "service_account" "$TMP/neg.err"; then
    fail "negative $name: the error echoes the value given as a ref"
  fi
done < <(python3 -c '
import json, sys
for c in json.load(open(sys.argv[1], encoding="utf-8"))["negative"]:
    print(c["name"] + "\t" + c["expect"])
' "$CASES")
if [[ $negatives -eq 0 ]]; then
  fail "no negative case was read from $CASES"
fi

printf '{"google":"%s"}\n' "$status"
[[ $status == pass ]]
