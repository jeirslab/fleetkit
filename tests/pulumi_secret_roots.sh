#!/usr/bin/env bash
# Secret roots declared by an estate's flake, as the runner reads them: an
# estate repo (a git repo made here) whose flake passes a flake input, the
# tenant's source, as `secretRoots` to lib.pulumi.stacks, at two commits that
# pin two tenant sources. Each commit is checked out on its own and evaluated
# the way the runner does it: `nix eval --json <checkout>#pulumi --apply
# <VIEW>`, VIEW being the expression in cli/fleetkit_cli/render.py, read from
# there. Each commit's root is its own tenant's source, a directory in the
# store holding that tenant's file; an estate that declares none has none and
# the same program file. Evaluation only, no network (path inputs). Prints
# {"pulumi_secret_roots":"pass"|"fail"}.
set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
status=pass
fail() { echo "FAIL $*" >&2; status=fail; }
g() { git -C "$TMP/estate" -c user.name=t -c user.email=t@example.com "$@"; }

VIEW=$(python3 - "$ROOT/cli/fleetkit_cli/render.py" <<'PY'
import ast, sys
for node in ast.parse(open(sys.argv[1]).read()).body:
    if isinstance(node, ast.Assign) and getattr(node.targets[0], "id", "") == "VIEW":
        print(ast.literal_eval(node.value))
PY
)
[[ $VIEW == *secretRoots* ]] || fail "render.VIEW does not read secretRoots: $VIEW"

# The kit as a flake input, without its git history or work files.
mkdir -p "$TMP/kit" "$TMP/estate"
git -C "$ROOT" ls-files -z | (cd "$ROOT" && xargs -0 cp --parents -t "$TMP/kit" 2>/dev/null)
for t in a b; do
  mkdir -p "$TMP/tenant-$t/secrets"
  echo "tenant: $t" >"$TMP/tenant-$t/secrets/tenant.yaml"
done

flake() { # <secretRoots argument, or empty>
  cat >"$TMP/estate/flake.nix" <<EOF
{
  inputs.fleetkit.url = "path:$TMP/kit";
  inputs.tenant = { url = "path:$TMP/tenant-${2:-a}"; flake = false; };
  outputs = { fleetkit, tenant, ... }: {
    pulumi = fleetkit.lib.pulumi.stacks {
      fleet = fleetkit.lib.fleet { modules = [ "\${fleetkit}/tests/fixtures/tf-mini" ]; };
      modules = [ (import "\${fleetkit}/tests/fixtures/pulumi-nix" { poolId = "extra"; }) ];
      $1
    };
  };
}
EOF
}
commit() { # message -> sha
  g add -A >&2 && nix flake lock "$TMP/estate" >&2 2>"$TMP/err" || { tail -n 20 "$TMP/err" >&2; fail "lock: $1"; }
  g add -A >&2 && g commit -qm "$1" >&2 && g rev-parse HEAD
}
view() { # sha -> the runner's view of that commit, from a checkout of it alone
  local d="$TMP/checkouts/$1"
  mkdir -p "$TMP/checkouts"
  git clone -q --shared --no-checkout "$TMP/estate" "$d" >&2 && git -C "$d" checkout -q --detach "$1" >&2
  nix eval --json "$d#pulumi" --apply "$VIEW" 2>"$TMP/err" || { tail -n 20 "$TMP/err" >&2; fail "eval at $1"; }
}

git init -q -b main "$TMP/estate"
flake "" a;                        none=$(commit "no roots")
flake "secretRoots = [ tenant ];" a; one=$(commit "tenant a")
flake "secretRoots = [ tenant ];" b; two=$(commit "tenant b")

python3 - "$(view "$none")" "$(view "$one")" "$(view "$two")" <<'PY' || fail "roots per commit"
import json, os, sys
none, one, two = (json.loads(a)["mini-guests"] for a in sys.argv[1:4])
assert none["secretRoots"] == [], none["secretRoots"]
# Declaring roots changes no program.
assert none["file"] == one["file"] == two["file"], (none["file"], one["file"], two["file"])
(a,), (b,) = one["secretRoots"], two["secretRoots"]
assert a != b and a.startswith("/nix/store/") and b.startswith("/nix/store/"), (a, b)
# The evaluation fetched each: the directory is there, with that commit's file.
for root, t in ((a, "a"), (b, "b")):
    assert open(os.path.join(root, "secrets/tenant.yaml")).read() == f"tenant: {t}\n", root
PY

printf '{"pulumi_secret_roots":"%s"}\n' "$status"
[[ $status == pass ]]
