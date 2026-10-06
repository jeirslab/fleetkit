#!/usr/bin/env bash
# tools/sops-config.py: renders the fixture report, compares with the expected
# text below, round-trips through --check, and requires a changed recipient to
# fail --check. One JSON line on stdout. Run by tools/gates.sh (lint gate).
set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT" || exit 2
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
TOOL=tools/sops-config.py
FIX=tests/fixtures/sops-report.json

# Stand-in for ssh-to-age so the test needs no extra tool.
cat >"$TMP/fake-ssh-to-age" <<'SH'
#!/usr/bin/env bash
cat >/dev/null
echo age1pppppppppppppppppppppppppppppppppppppppppppppppppppppppppp
SH
chmod +x "$TMP/fake-ssh-to-age"

cat >"$TMP/expected" <<'YAML'
keys:
  - &admin age1zzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzz
  - &host_app_1 age1qqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqq
  - &web age1pppppppppppppppppppppppppppppppppppppppppppppppppppppppppp
creation_rules:
  - path_regex: ^secrets/app\.yaml$
    key_groups:
      - age:
          - *admin
          - *host_app_1
  - path_regex: ^secrets/shared\.yaml$
    key_groups:
      - age:
          - *admin
          - *host_app_1
          - *web
  - path_regex: ^secrets/web\.yaml$
    key_groups:
      - age:
          - *admin
          - *web
YAML

fail=0
say() { printf 'sops_config: %s\n' "$*" >&2; }
run() { python3 "$TOOL" --report "$FIX" --ssh-to-age "$TMP/fake-ssh-to-age" "$@"; }

run >"$TMP/out" 2>"$TMP/err" || { say "render failed"; cat "$TMP/err" >&2; fail=1; }
diff -u "$TMP/expected" "$TMP/out" >&2 || { say "output differs from expected"; fail=1; }
run >"$TMP/out2" 2>/dev/null; cmp -s "$TMP/out" "$TMP/out2" || { say "output not deterministic"; fail=1; }
run --check "$TMP/out" 2>"$TMP/err" || { say "round trip --check failed"; cat "$TMP/err" >&2; fail=1; }
sed 's/age1qqqq/age1qqqx/' "$TMP/out" >"$TMP/mut"
if run --check "$TMP/mut" 2>"$TMP/err"; then say "mutated recipient passed --check"; fail=1
elif ! grep -q host_app_1 "$TMP/err"; then say "--check did not name the anchor"; fail=1; fi
if python3 "$TOOL" --report "$FIX" --ssh-to-age /nonexistent/ssh-to-age >/dev/null 2>"$TMP/err" \
   || ! grep -q 'not found' "$TMP/err"; then say "missing ssh-to-age not reported"; fail=1; fi

if ((fail)); then echo '{"sops_config":"fail"}'; exit 1; fi
echo '{"sops_config":"pass"}'
