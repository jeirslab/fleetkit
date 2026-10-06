#!/usr/bin/env bash
# Secret-pattern scan. Rejects: forbidden paths, private-key material, non-ENC
# sops values, user:pass@ URLs. Reads blobs from the git index; never prints
# matched content, only file names and counts.
#   secrets-scan.sh            scan staged changes (pre-commit mode)
#   secrets-scan.sh --all      scan every file in the index (gate mode)
set -uo pipefail
cd "$(git rev-parse --show-toplevel)" || exit 2
bad=0
fail() { echo "secrets-scan: $1" >&2; bad=1; }

if [[ "${1:-}" == "--all" ]]; then
  list() { git ls-files -z; }
else
  list() { git diff --cached --name-only --diff-filter=ACMR -z; }
fi

while IFS= read -r -d "" f; do
  case "$f" in
    *.env.example) ;;
    .fleet/*|sources/*|.l3/*|.env|.env.*|*/.env|*/.env.*)
      fail "forbidden path staged: $f"; continue ;;
    *.dec.*|*.decrypted*|*.tfstate|*.tfstate.backup)
      fail "forbidden path (decrypted output or tofu state): $f"; continue ;;
  esac
  # This script and the tests of it name the patterns; skip self only.
  [[ "$f" == tools/hooks/secrets-scan.sh ]] && continue
  blob="$(git show ":$f" 2>/dev/null)" || continue
  if printf '%s\n' "$blob" | rg -q -e 'AGE-SECRET-KEY' -e 'BEGIN [A-Z ]*PRIVATE KEY'; then
    fail "private key material in $f"
  fi
  # The literal documentation placeholder "username:password@" (vendor
  # provider schemas under providers/schemas/ use it in descriptions) is not a
  # credential; every other user:pass@ URL is.
  if printf '%s\n' "$blob" | rg -o -e '[a-zA-Z][a-zA-Z0-9+.-]*://[^/[:space:]:@]+:[^/[:space:]@]+@' \
    | rg -q -v -e '://username:password@$'; then
    fail "user:pass@ URL in $f"
  fi
  # Provider tokens: GitHub, Tailscale, Slack, AWS access key ids.
  if printf '%s\n' "$blob" | rg -q \
    -e '(ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}' \
    -e 'github_pat_[A-Za-z0-9_]{20,}' \
    -e 'tskey-(auth|api|client)-[A-Za-z0-9-]{8,}' \
    -e 'xox[abp]-[A-Za-z0-9-]{10,}' \
    -e 'AKIA[0-9A-Z]{16}'; then
    fail "provider token in $f"
  fi
  # Nix signing secret key: <name>:<86 base64 chars>== (public keys are 43+=).
  if printf '%s\n' "$blob" | rg -q -e '[A-Za-z0-9.-]+:[A-Za-z0-9+/]{86}=='; then
    fail "nix signing secret key in $f"
  fi
  # libpq keyword connection strings: password=... / sslpassword=... with a
  # literal value (not a $var, {template} or <placeholder>, not ENC[...]).
  if printf '%s\n' "$blob" \
    | rg -o -e '(^|[^A-Za-z0-9_])(ssl)?password=[^[:space:]$<{&;",.)`][^[:space:]]*' \
    | rg -q -v -e 'password=ENC\['; then
    fail "password= in keyword connection string in $f"
  fi
  # sops JSON: top-level "sops" object; every leaf outside it must be ENC[...].
  if printf '%s\n' "$blob" | rg -q -e '"sops"[[:space:]]*:'; then
    leaked="$(printf '%s\n' "$blob" | python3 -c '
import json, sys
try:
    d = json.load(sys.stdin)
except Exception:
    print(0); sys.exit()
if not (isinstance(d, dict) and isinstance(d.get("sops"), dict)):
    print(0); sys.exit()
n = 0
def walk(v):
    global n
    if isinstance(v, dict):
        for x in v.values(): walk(x)
    elif isinstance(v, list):
        for x in v: walk(x)
    elif not (isinstance(v, str) and v.startswith("ENC[")):
        n += 1
for k, v in d.items():
    if k != "sops": walk(v)
print(n)')"
    if [ "${leaked:-0}" != "0" ]; then
      fail "$leaked non-ENC value(s) in sops JSON file $f"
    fi
  fi
  # sops dotenv: sops_version= marks it; every other KEY=VALUE must be ENC[...].
  if printf '%s\n' "$blob" | rg -q -e '^sops_version='; then
    leaked="$(printf '%s\n' "$blob" | awk '
      /^[[:space:]]*#/ || /^[[:space:]]*$/ || /^sops_/ { next }
      /^[A-Za-z_][A-Za-z0-9_.-]*=ENC\[/ { next }
      /^[A-Za-z_][A-Za-z0-9_.-]*=/ { n++ }
      END { print n+0 }')"
    if [ "$leaked" != "0" ]; then
      fail "$leaked non-ENC value line(s) in sops dotenv file $f"
    fi
  fi
  # sops files: every value before the trailing "sops:" metadata block must be ENC[...].
  if printf '%s\n' "$blob" | rg -q '^sops:'; then
    leaked="$(printf '%s\n' "$blob" | awk '
      /^sops:/ { exit }
      /^[[:space:]]*#/ || /^---/ || /^[[:space:]]*$/ { next }
      /^[[:space:]]*(- )?[A-Za-z0-9_."-]+:[[:space:]]*$/ { next }
      /ENC\[/ { next }
      /^[[:space:]]*(- )?[A-Za-z0-9_."-]+:[[:space:]]+[^[:space:]]/ { n++ }
      END { print n+0 }')"
    if [ "$leaked" != "0" ]; then
      fail "$leaked non-ENC value line(s) in sops file $f"
    fi
  fi
done < <(list)
exit $bad
