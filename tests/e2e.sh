#!/usr/bin/env bash
# End to end, with keys generated here and thrown away: a data repository from
# tests/fixtures/e2e, its registry evaluated by lib/secrets/eval-registry.nix (nix-instantiate,
# dummy store), and the secrets CLI against real sops and age.
#   render, encrypt, check passes; decryption follows the readers;
#   check fails on a registry key missing from its file, an edited .sops.yaml,
#   a plaintext file, a reader that is not a recipient, two keys at one
#   Infisical destination; rekey fixes a reader; host add records a new host
#   key and re-keys what it reads; a protected file is never re-encrypted;
#   allow excuses known drift (a missing file too); no value ever reaches the CLI's output.
# Decryption checks write to /dev/null. Needs NIXPKGS_LIB (nixpkgs' lib/).
# One JSON line on stdout.
set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
: "${NIXPKGS_LIB:?set NIXPKGS_LIB to nixpkgs/lib}"
T="$(mktemp -d)"; trap 'rm -rf "$T"' EXIT
K="$T/keys" R="$T/repo" LOG="$T/cli.log"
mkdir -p "$K" "$R/secrets" "$T/nix"
fail=0
say() { printf 'e2e: %s\n' "$*" >&2; }
bad() { say "FAIL $*"; fail=1; }

# --- keys (thrown away with $T) ---------------------------------------------
age-keygen -o "$K/admin.key" 2>/dev/null
ADMIN="$(age-keygen -y "$K/admin.key")"
for h in web web2 db_old db_new; do
  ssh-keygen -q -t ed25519 -N '' -C '' -f "$K/$h"
  ssh-to-age -private-key -i "$K/$h" -o "$K/$h.age"
done
pub() { cut -d' ' -f1-2 "$K/$1.pub"; }

# --- the repository ------------------------------------------------------------
cp "$ROOT/tests/fixtures/e2e/registry.nix" "$R/"
python3 - "$R" "$ADMIN" "$(pub web)" "$(pub db_old)" <<'PY'
import json, sys
r, admin, web, db = sys.argv[1:]
json.dump({"admin": {"age": admin}}, open(f"{r}/operators.json", "w"))
json.dump({"apps/web": {"ed25519": web}, "apps/db": {"ed25519": db}}, open(f"{r}/hosts.json", "w"))
PY
cd "$R" || exit 2
git init -q .

cat >"$T/eval" <<SH
#!/usr/bin/env bash
export NIX_STATE_DIR="$T/nix/state" NIX_LOG_DIR="$T/nix/log" NIX_CONF_DIR="$T/nix" HOME="$T/nix"
exec nix-instantiate --store dummy:// --eval --strict --json -E \
  "let lib = import $NIXPKGS_LIB; in (import $ROOT/lib/secrets/eval-registry.nix { inherit lib; } { registry = \$1/registry.nix; }).checkReport { }"
SH
chmod +x "$T/eval"
export SECRETS_EVAL="$T/eval" SOPS_AGE_KEY_FILE="$K/admin.key"
S() { python3 "$ROOT/tools/secrets" "$@" >>"$LOG" 2>&1; }
check() { python3 "$ROOT/tools/secrets" check >"$T/out" 2>"$T/err"; local rc=$?; cat "$T/out" "$T/err" >>"$LOG"; return $rc; }
dec() { SOPS_AGE_KEY_FILE="$K/$1.age" sops decrypt "$2" >/dev/null 2>&1; }

# --- render, encrypt, check -------------------------------------------------------
S render || bad "render"
grep -q '^  - &admin age1' .sops.yaml && grep -q '^  - &host_web age1' .sops.yaml || bad ".sops.yaml lacks the anchors"
rand() { head -c 16 /dev/urandom | od -An -tx1 | tr -d ' \n'; }
V1="$(rand)" V2="$(rand)" V3="$(rand)" V4="$(rand)"
printf 'token: %s\ndb:\n    password: %s\n' "$V1" "$V2" >secrets/web.yaml
printf 'password: %s\n' "$V3" >secrets/db.yaml
printf 'profile: %s\n' "$V4" >secrets/vpn.yaml
for f in web db vpn; do sops encrypt -i "secrets/$f.yaml" 2>>"$LOG" || bad "encrypt $f"; done
git add -A
check || { bad "check of a fresh repository"; cat "$T/err" >&2; }
dec web secrets/web.yaml || bad "web cannot decrypt web.yaml"
dec web secrets/db.yaml && bad "web decrypts db.yaml, which it does not read"
dec db_old secrets/db.yaml || bad "db cannot decrypt db.yaml"

# --- check fails, naming the cause --------------------------------------------------
touch db-extra-key
check && bad "a registry key missing from its file passed"
grep -q 'declared key extra is not in the file' "$T/err" || bad "missing key not named"
rm db-extra-key

cp .sops.yaml "$T/sops.bak"; sed -i 's/^# e2e$/# edited/' .sops.yaml
check && bad "an edited .sops.yaml passed"
grep -q 'not what the registry renders' "$T/err" || bad "edited .sops.yaml not named"
cp "$T/sops.bak" .sops.yaml

printf 'pw: %s\nsops:\n  age: []\n' "plain$(rand)" >secrets/leak.yaml; git add -A
check && bad "a plaintext sops file passed"
grep -q 'non-ENC value' "$T/err" || bad "plaintext not named"
git rm -q --cached secrets/leak.yaml; rm secrets/leak.yaml

printf '{ lib, ... }: { imports = [ ./registry.nix ]; fleet.estates.apps.secrets.files.db.placements.password.infisical = { project = "apps"; name = "token"; }; }\n' >dup.nix
mv registry.nix base.nix; sed 's#\./registry\.nix#./base.nix#' dup.nix >registry.nix
check && bad "two keys at one Infisical destination passed"
grep -q 'same Infisical destination' "$T/err" || bad "Infisical clash not named"
mv base.nix registry.nix; rm dup.nix

# --- a new reader: check fails until rekey ---------------------------------------
touch web-reads-db; S render
check && bad "a reader that is not a recipient passed"
grep -q 'secrets/web.yaml: not encrypted to host_db' "$T/err" || bad "missing recipient not named"
python3 "$ROOT/tools/secrets" rekey --dry-run >"$T/dry" 2>>"$LOG"
[ "$(cat "$T/dry")" = "secrets/web.yaml" ] || bad "rekey --dry-run listed: $(tr '\n' ' ' <"$T/dry")"
S rekey || bad "rekey"
check || bad "check after rekey"
dec db_old secrets/web.yaml || bad "the new reader cannot decrypt after rekey"
rm web-reads-db; S render; S rekey
check || bad "check after removing the reader"
dec db_old secrets/web.yaml && bad "a removed reader still decrypts after rekey"

# --- host add: a new host key, the files it reads re-keyed -----------------------
printf '#!/bin/sh\necho "db.invalid %s"\n' "$(pub db_new)" >"$T/keyscan"; chmod +x "$T/keyscan"
S host add apps/db db.invalid --keyscan "$T/keyscan" || bad "host add"
python3 -c 'import json,sys; sys.exit(0 if json.load(open("hosts.json"))["apps/db"]["ed25519"] == sys.argv[1] else 1)' "$(pub db_new)" \
  || bad "hosts.json does not hold the new key"
dec db_new secrets/db.yaml || bad "the new host key cannot decrypt db.yaml"
dec db_old secrets/db.yaml && bad "the old host key still decrypts db.yaml"
git add -A
check || { bad "check after host add"; cat "$T/err" >&2; }

# --- protected files are never re-encrypted --------------------------------------
before="$(sha256sum secrets/vpn.yaml)"
printf '#!/bin/sh\necho "web.invalid %s"\n' "$(pub web2)" >"$T/keyscan"
S host add apps/web web.invalid --keyscan "$T/keyscan" || bad "host add apps/web"
[ "$(sha256sum secrets/vpn.yaml)" = "$before" ] || bad "host add re-encrypted a protected file"
S rekey secrets/vpn.yaml
[ "$(sha256sum secrets/vpn.yaml)" = "$before" ] || bad "rekey re-encrypted a protected file"
grep -q 'secrets/vpn.yaml: protected' "$LOG" || bad "protected file not reported"
dec web2 secrets/web.yaml || bad "web's new key cannot decrypt web.yaml"
git add -A
check && bad "a protected file that a reader cannot decrypt passed"
grep -q 'secrets/vpn.yaml: not encrypted to host_web' "$T/err" || bad "protected drift not named"

# --- allow excuses known drift -----------------------------------------------------
"$T/eval" "$R" >"$T/report.json"
python3 - "$T/report.json" <<'PY'
import json, sys
d = json.load(open(sys.argv[1])); d["allow"]["recipients"] = ["secrets/vpn.yaml"]
json.dump(d, open(sys.argv[1], "w"))
PY
python3 "$ROOT/tools/secrets-check.py" --report "$T/report.json" --root "$R" >"$T/out" 2>"$T/err" \
  || { bad "allow did not excuse the protected file"; cat "$T/err" >&2; }
grep -q '^excused recipients: secrets/vpn.yaml' "$T/err" || bad "excused drift not listed"

# A file the registry names that does not exist fails, unless every key it declares is allowed.
python3 - "$T/report.json" <<'PY'
import json, sys
d = json.load(open(sys.argv[1]))
d["files"].append({"estate": "apps", "file": "gone", "path": "secrets/gone.yaml", "keys": ["a", "b"],
                   "readers": [], "operators": []})
d["allow"]["missingKeys"] = ["secrets/gone.yaml#a"]
json.dump(d, open(sys.argv[1], "w"))
PY
python3 "$ROOT/tools/secrets-check.py" --report "$T/report.json" --root "$R" >"$T/out" 2>"$T/err" \
  && bad "a missing file with one key not allowed passed"
grep -q '^FAIL keys: apps/gone: secrets/gone.yaml does not exist' "$T/err" || bad "missing file not named"
python3 - "$T/report.json" <<'PY'
import json, sys
d = json.load(open(sys.argv[1])); d["allow"]["missingKeys"] += ["secrets/gone.yaml#b"]
json.dump(d, open(sys.argv[1], "w"))
PY
python3 "$ROOT/tools/secrets-check.py" --report "$T/report.json" --root "$R" >"$T/out" 2>"$T/err" \
  || { bad "allow did not excuse the missing file"; cat "$T/err" >&2; }
grep -q '^excused keys: apps/gone: secrets/gone.yaml does not exist' "$T/err" || bad "excused missing file not listed"

# --- no value in anything the CLI printed ----------------------------------------
for v in "$V1" "$V2" "$V3" "$V4"; do
  grep -qF "$v" "$LOG" "$T/out" "$T/err" && bad "a secret value reached the CLI's output"
done

if ((fail)); then echo '{"e2e":"fail"}'; exit 1; fi
echo '{"e2e":"pass"}'
