#!/usr/bin/env bash
# Experimental. Renders lib.mkPulumi for the estates of tests/fixtures/tf-mini
# ("gaps" among them: every option added for adopting existing guests; and
# "quiet": adoption.unrecorded, whose ignored arguments must be these
# options.ignoreChanges, in the bridge's names)
# and lib.mkGithubPulumi for gh-mini, and checks each against its Terraform
# render and the pinned Pulumi schemas (tests/pulumi.py); that the estates
# the internal stage refuses (split, offsite) are refused by mkPulumi with a
# message naming it; that no render carries `import`, with adopt = true too,
# and that the adoption ids of the same fixtures as Pulumi.nix stacks
# (lib.pulumi.fromModel) are, key by key, tests/fixtures/adopt-expected.json
# (tests/pulumi_adopt.py; gh-mini also with tests/fixtures/gh-adopt, which
# renders every GitHub resource type the kit has); and that the name maps are
# what tests/gen_pulumi_names.py makes from the pinned schemas. Evaluation
# only. Prints one JSON line:
# {"pulumi":"pass"|"fail"}; exit 0 iff pass.
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

render() { # fn fixture estate -> $TMP/<estate>.<fn>.json, or the error in .err
  nix eval --impure --json "$ROOT#lib" --apply "l: l.${1//_/.} {
    fleet = l.fleet { modules = [ $ROOT/tests/fixtures/$2 ]; };
    estate = \"$3\";
  }" >"$TMP/$3.$1.json" 2>"$TMP/$3.$1.err"
}

status=pass
pair() { # tfFn pulumiFn fixture estate
  if render "$1" "$3" "$4" && render "$2" "$3" "$4"; then
    python3 "$ROOT/tests/pulumi.py" "$TMP/$4.$2.json" "$TMP/$4.$1.json" || status=fail
  else
    tail -n 40 "$TMP/$4.$1.err" "$TMP/$4.$2.err" >&2
    status=fail
  fi
}
for estate in mini tenant bare gaps quiet; do
  pair internal_guests mkPulumi tf-mini "$estate"
done

# adoption.unrecorded = "ignore" in the Pulumi program: exactly these paths,
# in the bridge's names, and none on the guests that set it back to "apply".
if [[ -s $TMP/quiet.mkPulumi.json ]] && ! python3 - "$TMP/quiet.mkPulumi.json" <<'PY' >&2; then
import json, sys
res = json.load(open(sys.argv[1]))["resources"]
ct = ["cpu", "memory", "vmId", "console"]
want = {
    "ct": ct,
    "machine": ["cpu", "memory", "scsiHardware", "agent", "operatingSystem", "efiDisk"],
    "mixed": ["operatingSystem", "cpu", "memory", "vmId", "console"],
    "loudct": None,
    "loudvm": None,
}
bad = [f"{k}: options.ignoreChanges is {(res.get(k, {}).get('options') or {}).get('ignoreChanges')}, expected {v}"
       for k, v in want.items() if (res.get(k, {}).get("options") or {}).get("ignoreChanges") != v]
bad += [f"{k}: {p} is ignored but no longer rendered" for k in ("ct", "mixed") for p in ("vmId", "console")
        if p not in res.get(k, {}).get("properties", {})]
for b in bad:
    print(f"pulumi: quiet: {b}", file=sys.stderr)
sys.exit(1 if bad else 0)
PY
  status=fail
fi
pair internal_github mkGithubPulumi gh-mini gh

# What the internal stage refuses, mkPulumi refuses, naming mkPulumi.
for estate in split offsite; do
  if render mkPulumi tf-mini "$estate"; then
    echo "FAIL $estate: mkPulumi rendered what the internal stage refuses" >&2
    status=fail
  elif ! grep -q "mkPulumi: fleet\.estates\.$estate: " "$TMP/$estate.mkPulumi.err"; then
    echo "FAIL $estate: mkPulumi's error does not name it" >&2
    tail -n 20 "$TMP/$estate.mkPulumi.err" >&2
    status=fail
  fi
done

# adopt = true: the ids are beside each resource (`adopt`, `adoptUnresolved`),
# and neither that render nor the default one has an `import` anywhere.
for spec in "mkPulumi tf-mini mini" "mkGithubPulumi gh-mini gh"; do
  read -r fn fixture estate <<<"$spec"
  if ! nix eval --impure --json "$ROOT#lib" --apply "l: l.$fn {
      fleet = l.fleet { modules = [ $ROOT/tests/fixtures/$fixture ]; };
      estate = \"$estate\"; adopt = true;
    }" >"$TMP/$estate.adopt.json" 2>"$TMP/$estate.adopt.err"; then
    tail -n 20 "$TMP/$estate.adopt.err" >&2
    status=fail
  elif ! python3 - "$TMP/$estate.adopt.json" "$TMP/$estate.$fn.json" <<'PY' >&2; then
import json, sys
with_ids, plain = (json.load(open(f)) for f in sys.argv[1:3])
bad = []
for label, prog in (("adopt = true", with_ids), ("default", plain)):
    if '"import"' in json.dumps(prog):
        bad.append(f"{label}: the render names import")
if '"adopt' in json.dumps(plain):
    bad.append("default: the render carries adoption data")
carried = [k for k, r in with_ids["resources"].items() if "adopt" in r or "adoptUnresolved" in r]
if not carried:
    bad.append("adopt = true: no resource carries adopt or adoptUnresolved")
strip = lambda r: {k: v for k, v in r.items() if k not in ("adopt", "adoptUnresolved")}
if {k: strip(r) for k, r in with_ids["resources"].items()} != plain["resources"]:
    bad.append("adopt = true changes more than the adoption fields")
for b in bad:
    print(f"pulumi: {sys.argv[1]}: {b}", file=sys.stderr)
sys.exit(1 if bad else 0)
PY
    status=fail
  fi
done

# The same fixtures as Pulumi.nix stacks: adoptIds / adoptUnresolved.
GH_TYPES=github_repository,github_branch_default,github_membership,github_team,github_team_members
GH_TYPES+=,github_team_repository,github_organization_settings,github_actions_organization_permissions
GH_TYPES+=,github_repository_environment,github_organization_ruleset,github_issue_label
GH_TYPES+=,github_actions_variable,github_repository_file,github_actions_secret
GH_TYPES+=,github_actions_organization_secret
stacks() { # name case types modules-of-the-model fromModel-args...
  local name="$1" case="$2" types="$3" model="$4" mods="" a
  shift 4
  for a in "$@"; do mods="$mods (kit.lib.pulumi.fromModel { $a })"; done
  if nix eval --impure --json --expr "
      let
        kit = builtins.getFlake (toString $ROOT);
        s = kit.lib.pulumi.stacks {
          fleet = kit.lib.fleet { modules = [ $model ]; };
          modules = [ $mods ];
        };
      in builtins.mapAttrs (_: x: { inherit (x) program file adoptIds adoptUnresolved; }) s
    " >"$TMP/stacks.$name.json" 2>"$TMP/stacks.$name.err"; then
    python3 "$ROOT/tests/pulumi_adopt.py" "$TMP/stacks.$name.json" "$case" "$types" || status=fail
  else
    tail -n 40 "$TMP/stacks.$name.err" >&2
    status=fail
  fi
}
stacks tf "" "" "$ROOT/tests/fixtures/tf-mini" \
  'estate = "mini";' 'estate = "tenant";' 'estate = "bare";' 'estate = "gaps";'
stacks gh "" "" "$ROOT/tests/fixtures/gh-mini" 'estate = "gh"; github = true;'
stacks gh-adopt "+gh-adopt" "$GH_TYPES" "$ROOT/tests/fixtures/gh-mini $ROOT/tests/fixtures/gh-adopt" \
  'estate = "gh"; github = true;'

# fromModel { adopt = false; }: no ids at all.
if ! out=$(nix eval --impure --json --expr "
    let
      kit = builtins.getFlake (toString $ROOT);
      s = (kit.lib.pulumi.stacks {
        fleet = kit.lib.fleet { modules = [ $ROOT/tests/fixtures/tf-mini ]; };
        modules = [ (kit.lib.pulumi.fromModel { estate = \"mini\"; adopt = false; }) ];
      }).mini-guests;
    in s.adoptIds // s.adoptUnresolved" 2>"$TMP/noadopt.err") || [[ $out != "{}" ]]; then
  echo "FAIL fromModel { adopt = false; }: ${out:-$(tail -n 5 "$TMP/noadopt.err")}" >&2
  status=fail
fi

# The name maps are regenerated from the pinned schemas, not hand-edited.
cp -r "$ROOT/providers/pulumi/names" "$TMP/names.before"
if ! python3 "$ROOT/tests/gen_pulumi_names.py" 2>"$TMP/gen.err"; then
  cat "$TMP/gen.err" >&2
  status=fail
elif ! diff -r "$TMP/names.before" "$ROOT/providers/pulumi/names" >&2; then
  echo "FAIL: providers/pulumi/names differ from tests/gen_pulumi_names.py output" >&2
  status=fail
fi

printf '{"pulumi":"%s"}\n' "$status"
[[ $status == pass ]]
