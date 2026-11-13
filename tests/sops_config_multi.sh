#!/usr/bin/env bash
# tools/sops-config.py --config (ADR-0009): renders the multi-estate fixture
# (tests/fixtures/sops-config.json, what tests/fixtures/registry.nix evaluates
# to) to the expected text below; --check passes on it and fails on a changed
# recipient (naming the anchor) or a changed comment (with a diff); an unknown
# or doubled anchor is refused; --import reads a hand-written .sops.yaml, with
# comments, into the layout that renders it back. One JSON line on stdout.
set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT" || exit 2
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
TOOL=tools/sops-config.py
FIX=tests/fixtures/sops-config.json

cat >"$TMP/fake-ssh-to-age" <<'SH'
#!/usr/bin/env bash
cat >/dev/null
echo age1dddddddddddddddddddddddddddddddddddddddddddddddddddddddddd
SH
chmod +x "$TMP/fake-ssh-to-age"

cat >"$TMP/expected" <<'YAML'
# Rendered from the registry. Do not edit: run `secrets render`.
keys:
  - &admin age1zzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzz
  - &host_db age1dddddddddddddddddddddddddddddddddddddddddddddddddddddddddd
  - &host_web age1wwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwww
  - &host_node1 age1nnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnn
  - &ops_admin age1pppppppppppppppppppppppppppppppppppppppppppppppppppppppppp
  # legacy/.sops.yaml, anchors prefixed legacy_
  - &legacy_master age1mmmmmmmmmmmmmmmmmmmmmmmmmmmmmmmmmmmmmmmmmmmmmmmmmmmmmmmmmm
  - &legacy_app_host age1aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
creation_rules:
  - path_regex: ^apps/db\.yaml$
    key_groups:
      - age:
          - *admin
          - *host_db
          - *host_web
  - path_regex: ^apps/web\.yaml$
    key_groups:
      - age:
          - *admin
          - *host_web
  - path_regex: ^ops/node1\.yaml$
    key_groups:
      - age:
          - *host_node1
          - *ops_admin
  # legacy/.sops.yaml, in its order, re-rooted under legacy/
  - path_regex: ^legacy/.*secrets/vpn\.yaml$
    key_groups:
      - age:
          - *legacy_master
  - path_regex: ^legacy/secrets/app\.yaml$
    key_groups:
      - age:
          - *legacy_master
          - *legacy_app_host
YAML

fail=0
say() { printf 'sops_config_multi: %s\n' "$*" >&2; }
run() { python3 "$TOOL" --config "$FIX" --ssh-to-age "$TMP/fake-ssh-to-age" "$@"; }

run >"$TMP/out" 2>"$TMP/err" || { say "render failed"; cat "$TMP/err" >&2; fail=1; }
diff -u "$TMP/expected" "$TMP/out" >&2 || { say "output differs from expected"; fail=1; }
run >"$TMP/out2" 2>/dev/null; cmp -s "$TMP/out" "$TMP/out2" || { say "output not deterministic"; fail=1; }
run --check "$TMP/out" 2>"$TMP/err" || { say "round trip --check failed"; cat "$TMP/err" >&2; fail=1; }

sed 's/age1mmmm/age1mmmc/' "$TMP/out" >"$TMP/mut"
if run --check "$TMP/mut" 2>"$TMP/err"; then say "mutated recipient passed --check"; fail=1
elif ! grep -q legacy_master "$TMP/err"; then say "--check did not name the anchor"; fail=1; fi

sed 's/anchors prefixed legacy_/anchors prefixed/' "$TMP/out" >"$TMP/mut"
if run --check "$TMP/mut" 2>"$TMP/err"; then say "changed comment passed --check"; fail=1
elif ! grep -q '^-  # legacy/.sops.yaml, anchors prefixed$' "$TMP/err"; then say "--check did not show the diff"; fail=1; fi

python3 - "$FIX" "$TMP/unknown.json" "$TMP/double.json" <<'PY'
import json, sys
d = json.load(open(sys.argv[1]))
u = json.loads(json.dumps(d)); u["estates"][2]["rules"][0]["anchors"] = ["nobody"]
json.dump(u, open(sys.argv[2], "w"))
b = json.loads(json.dumps(d)); b["estates"][2]["prefix"] = ""; b["estates"][2]["keys"][0]["name"] = "admin"
json.dump(b, open(sys.argv[3], "w"))
PY
if python3 "$TOOL" --config "$TMP/unknown.json" --ssh-to-age "$TMP/fake-ssh-to-age" >/dev/null 2>"$TMP/err" \
   || ! grep -q "unknown anchor(s) nobody" "$TMP/err"; then say "unknown anchor not refused"; fail=1; fi
if python3 "$TOOL" --config "$TMP/double.json" --ssh-to-age "$TMP/fake-ssh-to-age" >/dev/null 2>"$TMP/err" \
   || ! grep -q "anchor 'admin' is declared twice" "$TMP/err"; then say "doubled anchor not refused"; fail=1; fi

# --import: a hand-written file, with comments, read back as the layout that
# the fixture's legacy estate declares.
cat >"$TMP/hand.sops.yaml" <<'YAML'
# who can decrypt what
keys:
  - &master age1mmmmmmmmmmmmmmmmmmmmmmmmmmmmmmmmmmmmmmmmmmmmmmmmmmmmmmmmmm   # the operator
  # application hosts
  - &app_host age1aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
creation_rules:
  - path_regex: secrets/vpn\.yaml$
    key_groups:
      - age:
          - *master
  # the application
  - path_regex: ^secrets/app\.yaml$
    key_groups:
    - age:
      - *master
      - *app_host
YAML
python3 "$TOOL" --import "$TMP/hand.sops.yaml" >"$TMP/layout.json" 2>"$TMP/err" || { say "--import failed"; cat "$TMP/err" >&2; fail=1; }
python3 - "$FIX" "$TMP/layout.json" <<'PY' || { say "--import does not give the fixture's layout"; fail=1; }
import json, sys
want = json.load(open(sys.argv[1]))["estates"][2]
got = json.load(open(sys.argv[2]))
sys.exit(0 if got == {"keys": want["keys"], "rules": want["rules"]} else 1)
PY
printf 'keys:\n  - &a age1aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\ncreation_rules:\n  - path_regex: x$\n    key_groups:\n      - age:\n          - *b\n' >"$TMP/bad.yaml"
if python3 "$TOOL" --import "$TMP/bad.yaml" >/dev/null 2>"$TMP/err" || ! grep -q "unknown anchor b" "$TMP/err"; then
  say "--import accepted an unknown anchor"; fail=1; fi

if ((fail)); then echo '{"sops_config_multi":"fail"}'; exit 1; fi
echo '{"sops_config_multi":"pass"}'
