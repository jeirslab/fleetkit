#!/usr/bin/env bash
# Renders lib.mkWorkflowCaller and checks: the text parses with a strict parser
# for exactly the YAML subset the renderer emits (block mappings, JSON scalars
# and flow values; python3's stdlib has no YAML parser), the job names the
# workflow at the given SHA, secrets and inputs round-trip, and a branch name,
# a tag and a short SHA are refused. The mkWorkflowCaller example in
# docs/pipeline.md is evaluated too, so the documentation cannot drift from
# the function. Every .github/workflows/*.yml and every action.yml under
# .github/actions must parse as YAML with yq from the flake's pinned nixpkgs;
# a parser that cannot be had fails the test. Also runs actionlint over every
# workflow file in .github/workflows that has `workflow_call`: the one on
# PATH, else the one from the pinned nixpkgs; if neither can be had it prints
# an explicit SKIPPED line (never a silent pass).
# Evaluation only. Prints {"workflow_caller":"pass"|"fail"}; exit 0 iff pass.
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
SHA=0123456789abcdef0123456789abcdef01234567

status=pass
ARGS=$(cat <<'NIX'
{
  name = "Check flake";
  workflow = "check-flake";
  ref = "@SHA@";
  on = {
    pull_request = { };
    push.branches = [ "main" ];
  };
  "with" = {
    runs_on = ''["self-hosted","nix"]'';
    note = "a \"quoted\" value: with # chars";
    retries = 3;
  };
  secrets.STATUS_TOKEN = "\${{ secrets.STATUS_TOKEN }}";
  permissions = {
    contents = "read";
    statuses = "write";
  };
}
NIX
)

render() { # ARGS-FILE-EXPR -> stdout
  nix eval --raw "$ROOT#lib" --apply "l: l.mkWorkflowCaller ($1)" 2>"$TMP/err"
}
render "${ARGS//@SHA@/$SHA}" >"$TMP/caller.yml" || { tail -n 20 "$TMP/err" >&2; status=fail; }
printf 'workflow_caller: rendered:\n' >&2
sed 's/^/  | /' "$TMP/caller.yml" >&2

if [[ $status == pass ]]; then
  python3 - "$TMP/caller.yml" "$SHA" <<'PY' >&2 || status=fail
import json, re, sys

def parse(text):
    """Strict parser for the emitted subset: 2-space block mappings; a value is
    either nothing (a nested mapping follows) or one JSON document."""
    root = {}
    stack = [(-2, root)]
    lines = text.split("\n")
    if lines[-1] != "":
        raise ValueError("no trailing newline")
    for n, line in enumerate(lines[:-1], 1):
        if not line.strip() or "\t" in line:
            raise ValueError(f"line {n}: blank or tab")
        indent = len(line) - len(line.lstrip(" "))
        if indent % 2:
            raise ValueError(f"line {n}: odd indent")
        m = re.fullmatch(r'("(?:[^"\\]|\\.)*"|[A-Za-z_][A-Za-z0-9_-]*):(?: (.+))?', line.strip())
        if not m:
            raise ValueError(f"line {n}: not 'key:' or 'key: json': {line!r}")
        key = json.loads(m[1]) if m[1][0] == '"' else m[1]
        while stack[-1][0] >= indent:
            stack.pop()
        parent = stack[-1][1]
        if indent != stack[-1][0] + 2:
            raise ValueError(f"line {n}: bad indent")
        if key in parent:
            raise ValueError(f"line {n}: duplicate key {key}")
        if m[2] is None:
            parent[key] = {}
            stack.append((indent, parent[key]))
        else:
            parent[key] = json.loads(m[2])
    return root

doc = parse(open(sys.argv[1]).read())
sha = sys.argv[2]
want = {
    "name": "Check flake",
    "on": {"pull_request": {}, "push": {"branches": ["main"]}},
    "permissions": {"contents": "read", "statuses": "write"},
    "jobs": {"call": {
        "uses": f"jeirslab/fleetkit/.github/workflows/check-flake.yml@{sha}",
        "with": {"runs_on": '["self-hosted","nix"]',
                 "note": 'a "quoted" value: with # chars', "retries": 3},
        "secrets": {"STATUS_TOKEN": "${{ secrets.STATUS_TOKEN }}"},
    }},
}
if doc != want:
    print("workflow_caller: parsed caller differs from the expected structure")
    print(json.dumps(doc, indent=1, sort_keys=True))
    sys.exit(1)
if not re.fullmatch(r".+/\.github/workflows/[^@/]+\.yml@[0-9a-f]{40}", doc["jobs"]["call"]["uses"]):
    print("workflow_caller: uses is not pinned to a 40 hex commit"); sys.exit(1)
# Selftest: the parser must reject what it does not understand.
for bad in ("a: b: [\n", "  a: 1\n", "a:\n   b: 1\n", "a: 1\na: 2\n", "a: 1"):
    try:
        parse(bad)
    except ValueError:
        continue
    print(f"workflow_caller: selftest: parser accepted {bad!r}"); sys.exit(1)
PY
fi

# A ref that is not a 40 hex commit must be an evaluation error naming `ref`.
for ref in main v1.0.0 0123456 0123456789ABCDEF0123456789ABCDEF01234567 "${SHA}0" ""; do
  if out=$(nix eval --raw "$ROOT#lib" --apply "l: l.mkWorkflowCaller { name = \"x\"; workflow = \"check-flake\"; ref = \"$ref\"; on.push = { }; }" 2>&1); then
    echo "workflow_caller: ref '$ref' was accepted" >&2
    status=fail
  elif ! grep -q 'option `ref`' <<<"$out"; then
    echo "workflow_caller: ref '$ref' refused without naming the option" >&2
    status=fail
  fi
done
# Unknown options are refused too (a typo must not silently drop a secret).
nix eval --raw "$ROOT#lib" --apply "l: l.mkWorkflowCaller { name = \"x\"; workflow = \"w\"; ref = \"$SHA\"; on.push = { }; secret = { }; }" >/dev/null 2>&1 &&
  { echo "workflow_caller: unknown option was accepted" >&2; status=fail; }

# The example in docs/pipeline.md: the first ```nix block, a binding of
# repos.<estate>.<key>.files.<path>.content, must evaluate to a caller.
DOC="$ROOT/docs/pipeline.md"
awk '/^```nix$/ && !done { on = 1; next } on && /^```$/ { on = 0; done = 1 } on' "$DOC" >"$TMP/doc.nix"
if ! grep -q 'mkWorkflowCaller' "$TMP/doc.nix"; then
  echo "workflow_caller: no mkWorkflowCaller example found in docs/pipeline.md" >&2
  status=fail
elif ! out=$(nix eval --raw "$ROOT#lib" --apply "l: let fleetkit.lib = l; in
    builtins.concatStringsSep \"\" (map (f: f.content) (builtins.attrValues ({
      $(cat "$TMP/doc.nix")
    }).repos.myestate.lab.files))" 2>"$TMP/err"); then
  echo "workflow_caller: the example in docs/pipeline.md does not evaluate" >&2
  tail -n 20 "$TMP/err" >&2
  status=fail
elif ! grep -Eq '^    uses: "jeirslab/fleetkit/\.github/workflows/[^@/"]+\.yml@[0-9a-f]{40}"$' <<<"$out"; then
  echo "workflow_caller: the example in docs/pipeline.md rendered no pinned uses line" >&2
  status=fail
else
  echo "workflow_caller: docs/pipeline.md example evaluates" >&2
fi

# Tools come from the nixpkgs the flake pins, so a run does not depend on PATH.
NIXPKGS="$(python3 -c 'import json, sys
n = json.load(open(sys.argv[1]))["nodes"]["nixpkgs"]["locked"]
print("github:%s/%s/%s" % (n["owner"], n["repo"], n["rev"]))' "$ROOT/flake.lock")" || NIXPKGS=

# Every workflow file and every action definition parses as YAML and is a
# mapping. This does not depend on actionlint and is never skipped.
mapfile -t YAMLFILES < <(
  find "$ROOT/.github/workflows" -maxdepth 1 -type f \( -name '*.yml' -o -name '*.yaml' \) 2>/dev/null | sort
  find "$ROOT/.github/actions" -type f \( -name 'action.yml' -o -name 'action.yaml' \) 2>/dev/null | sort
)
if ((${#YAMLFILES[@]} == 0)); then
  echo "workflow_caller: yaml FAIL: no workflow or action file found under .github" >&2
  status=fail
elif [[ -z $NIXPKGS ]] || ! nix shell "$NIXPKGS#yq-go" -c yq --version >/dev/null 2>"$TMP/err"; then
  echo "workflow_caller: yaml FAIL: no YAML parser (yq-go from the pinned nixpkgs)" >&2
  tail -n 20 "$TMP/err" >&2
  status=fail
else
  # Selftest: the parser must refuse broken YAML and the check a non-mapping.
  printf 'a: [1\nb: 2\n' >"$TMP/bad.yml"
  printf -- '- a\n' >"$TMP/list.yml"
  yaml_ok() { nix shell "$NIXPKGS#yq-go" -c yq -e 'tag == "!!map"' "$1" >/dev/null 2>>"$TMP/yaml.err"; }
  if yaml_ok "$TMP/bad.yml" || yaml_ok "$TMP/list.yml"; then
    echo "workflow_caller: yaml FAIL: selftest: broken input was accepted" >&2
    status=fail
  fi
  : >"$TMP/yaml.err"
  yaml=ok
  for f in "${YAMLFILES[@]}"; do
    if ! yaml_ok "$f"; then
      echo "workflow_caller: yaml FAIL ${f#"$ROOT"/}" >&2
      yaml=fail
      status=fail
    fi
  done
  cat "$TMP/yaml.err" >&2
  echo "workflow_caller: yaml parsed ${#YAMLFILES[@]} file(s): $yaml" >&2
fi

# actionlint over the reusable workflows: the one on PATH, else the pinned one.
mapfile -t REUSABLE < <(grep -l 'workflow_call' "$ROOT"/.github/workflows/*.yml 2>/dev/null)
ACTIONLINT=()
if command -v actionlint >/dev/null 2>&1; then
  ACTIONLINT=(actionlint)
elif [[ -n $NIXPKGS ]] && nix shell "$NIXPKGS#actionlint" "$NIXPKGS#shellcheck" -c actionlint -version >/dev/null 2>&1; then
  ACTIONLINT=(nix shell "$NIXPKGS#actionlint" "$NIXPKGS#shellcheck" -c actionlint)
fi
if ((${#ACTIONLINT[@]} == 0)); then
  echo "workflow_caller: actionlint SKIPPED: not on PATH and not available from the pinned nixpkgs (${#REUSABLE[@]} reusable workflow file(s) not linted)" >&2
elif ((${#REUSABLE[@]} == 0)); then
  echo "workflow_caller: actionlint SKIPPED: no workflow with workflow_call under .github/workflows" >&2
else
  if "${ACTIONLINT[@]}" "${REUSABLE[@]}" >&2; then
    echo "workflow_caller: actionlint ran on ${#REUSABLE[@]} file(s): ok" >&2
  else
    echo "workflow_caller: actionlint FAIL" >&2
    status=fail
  fi
fi

printf '{"workflow_caller":"%s"}\n' "$status"
[[ $status == pass ]]
