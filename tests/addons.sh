#!/usr/bin/env bash
# Estate add-ons (modules/addons.nix) and the Ansible add-on (lib/ansible.nix),
# on the estates of tests/fixtures/tf-mini. Checks that:
#   - no estate but "addon" renders anything of an add-on, as guests or as a
#     Pulumi program (no ansible provider, package or resource; nothing for
#     the runner to realise): an estate that switches none on is unchanged;
#   - the Pulumi program of "addon" runs each playbook after its guest
#     (options.dependsOn), replaces it on any change (replaceOnChanges "*":
#     the provider's update would run the old playbook; lib/pulumi.nix), and
#     uses the ansible/ansible provider through the bridge at its pin;
#   - its stack needs the two playbooks, and a playbook that is a derivation
#     is realised as <drv>^out and named in the program by its output path,
#     with the program still written by builtins.toFile (no build);
#   - each case of tests/cases-addons.json (the fixture plus one bad
#     declaration) is refused with its message ("expect", an extended regular
#     expression);
#   - providers/ansible/collections.json is of the ansible and ansible-core
#     the flake's nixpkgs pins (regenerate it with
#     tests/gen_ansible_collections.py when nixpkgs moves).
# Evaluation only. Prints one JSON line: {"addons":"pass"|"fail"}; exit 0 iff pass.
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
FIX="$ROOT/tests/fixtures/tf-mini"

status=pass
fail() {
  echo "FAIL $*" >&2
  status=fail
}

# Every estate of the fixture, guests and Pulumi program, and the stacks.
estates=(mini tenant bare gaps quiet tmpl addon)
if ! nix eval --impure --json --expr "
    let
      kit = builtins.getFlake (toString $ROOT);
      fleet = kit.lib.fleet { modules = [ $FIX ]; };
      es = [ $(printf '"%s" ' "${estates[@]}") ];
      s = kit.lib.pulumi.stacks { inherit fleet; modules = map (estate: kit.lib.pulumi.fromModel { inherit estate; }) es; };
    in builtins.listToAttrs (map (estate: {
      name = estate;
      value = {
        tf = kit.lib.internal.guests { inherit fleet estate; };
        pulumi = kit.lib.mkPulumi { inherit fleet estate; };
        stack = { inherit (s.\"\${estate}-guests\") needs realise; };
      };
    }) es)
  " >"$TMP/all.json" 2>"$TMP/all.err"; then
  tail -n 40 "$TMP/all.err" >&2
  fail "the fixture's estates do not render"
elif ! python3 - "$TMP/all.json" <<'PY' >&2; then
import json, re, sys
all_ = json.load(open(sys.argv[1]))
bad = []
for estate, r in all_.items():
    if estate == "addon":
        continue
    text = json.dumps({"tf": r["tf"], "pulumi": r["pulumi"]})
    if "ansible" in text:
        bad.append(f"{estate}: renders something of the Ansible add-on, which it does not switch on")
    if r["stack"] != {"needs": [], "realise": []}:
        bad.append(f"{estate}: its stack needs {r['stack']}, expected nothing")

a = all_["addon"]
res = a["pulumi"]["resources"]
want = {
    "vyos": {"dependsOn": ["${router}"], "replaceOnChanges": ["*"]},
    "tune": {"replaceOnChanges": ["*"]},
}
for k, opts in want.items():
    r = res.get(k) or {}
    if r.get("type") != "ansible:index/playbook:Playbook":
        bad.append(f"addon: {k} is {r.get('type')!r}, not an ansible Playbook")
    if (r.get("options") or {}) != opts:
        bad.append(f"addon: {k}: options are {r.get('options')}, expected {opts}")
# The guests are as without the add-on: no replaceOnChanges, their own keys.
for k in ("router", "provider-proxmox"):
    if "replaceOnChanges" in (res.get(k, {}).get("options") or {}):
        bad.append(f"addon: {k} is replaced on any change; only the playbooks are")
pkg = a["pulumi"]["packages"].get("ansible")
if pkg != {"source": "terraform-provider", "version": "1.4.0", "parameters": ["ansible/ansible", "1.5.0"]}:
    bad.append(f"addon: packages.ansible is {pkg}")
store = r"/nix/store/[0-9a-df-np-sv-z]{32}-"
pb = {k: res[k]["properties"]["playbook"] for k in want if k in res}
need = a["stack"]["needs"]
if sorted(need) != sorted([pb.get("vyos", "").removesuffix("/site.yml"), pb.get("tune")]):
    bad.append(f"addon: the stack needs {need}, not the two playbooks {pb}")
if a["stack"]["realise"] != a["stack"]["needs"] or not all(re.match(store, n) for n in need):
    bad.append(f"addon: realise is {a['stack']['realise']}, expected the playbooks' store paths")
for b in bad:
    print(f"addons: {b}", file=sys.stderr)
sys.exit(1 if bad else 0)
PY
  status=fail
fi

# A playbook that is a derivation: realised as <drv>^out, named by its output
# path in the program, and the program is still written (toFile takes no
# reference to an output). Nothing is built.
cat >"$TMP/drv.nix" <<'EOF'
{ lib, ... }:
{
  fleet.estates.addon.addons.ansible.playbooks.vyos.playbook = lib.mkForce (derivation {
    name = "vyos-playbook";
    system = "x86_64-linux";
    builder = "/bin/sh";
    args = [ "-c" "exit 1" ];
  });
}
EOF
if ! out=$(nix eval --impure --json --expr "
    let
      kit = builtins.getFlake (toString $ROOT);
      fleet = kit.lib.fleet { modules = [ $FIX $TMP/drv.nix ]; };
      s = (kit.lib.pulumi.stacks { inherit fleet; modules = [ (kit.lib.pulumi.fromModel { estate = \"addon\"; }) ]; }).addon-guests;
      d = fleet.estates.addon.addons.ansible.playbooks.vyos.playbook;
    in {
      ok = builtins.elem \"\${d.drvPath}^out\" s.realise
        && s.program.resources.vyos.properties.playbook == \"\${builtins.unsafeDiscardStringContext d.outPath}/site.yml\"
        && builtins.isString s.file;
      inherit (s) realise;
    }" 2>"$TMP/drv.err"); then
  tail -n 20 "$TMP/drv.err" >&2
  fail "a derivation as the playbook does not evaluate"
elif [[ $(python3 -c 'import json,sys; print(json.loads(sys.argv[1])["ok"])' "$out") != True ]]; then
  fail "a derivation as the playbook: $out"
fi

# Negative cases: the fixture plus one bad declaration each, rendered.
CASES="$ROOT/tests/cases-addons.json"
python3 - "$CASES" "$TMP" <<'PY' || status=fail
import json, sys
for c in json.load(open(sys.argv[1], encoding="utf-8"))["negative"]:
    open(f"{sys.argv[2]}/neg-{c['name']}.nix", "w", encoding="utf-8").write("\n".join(c["module"]) + "\n")
PY
negatives=0
while IFS=$'\t' read -r name expect; do
  negatives=$((negatives + 1))
  if nix eval --impure --json "$ROOT#lib" --apply "l: l.internal.guests {
      fleet = l.fleet { modules = [ $FIX $TMP/neg-$name.nix ]; };
      estate = \"addon\";
    }" >/dev/null 2>"$TMP/neg.err"; then
    fail "negative $name: evaluated, but must be refused"
  elif ! grep -Eq -- "$expect" "$TMP/neg.err"; then
    fail "negative $name: the error does not mention /$expect/"
    tail -n 8 "$TMP/neg.err" >&2
  fi
done < <(python3 -c '
import json, sys
for c in json.load(open(sys.argv[1], encoding="utf-8"))["negative"]:
    print(c["name"] + "\t" + c["expect"])
' "$CASES")
if [[ $negatives -eq 0 ]]; then
  fail "no negative case was read from $CASES"
fi

# The collections list is of the Ansible the flake pins (evaluation only).
if ! pinned=$(nix eval --impure --json --expr "
    let p = (builtins.getFlake (toString $ROOT)).inputs.nixpkgs.legacyPackages.x86_64-linux.python3Packages;
    in { ansible = p.ansible.version; ansible-core = p.ansible-core.version; }" 2>"$TMP/pin.err"); then
  tail -n 10 "$TMP/pin.err" >&2
  fail "the pinned ansible versions do not evaluate"
elif ! python3 - "$pinned" "$ROOT/providers/ansible/collections.json" <<'PY' >&2; then
import json, sys
pinned, listed = json.loads(sys.argv[1]), json.load(open(sys.argv[2]))
bad = [f"{k}: providers/ansible/collections.json is of {listed.get(k)}, the flake's nixpkgs has {v} "
       f"(tests/gen_ansible_collections.py)" for k, v in pinned.items() if listed.get(k) != v]
for need in ("vyos.vyos", "ansible.netcommon", "ansible.utils"):
    if need not in listed.get("collections", {}):
        bad.append(f"{need} is not in providers/ansible/collections.json")
for b in bad:
    print(f"addons: {b}", file=sys.stderr)
sys.exit(1 if bad else 0)
PY
  status=fail
fi

printf '{"addons":"%s"}\n' "$status"
[[ $status == pass ]]
