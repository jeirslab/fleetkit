#!/usr/bin/env bash
# Proves tools/hooks/secrets-scan.sh rejects each leak class and accepts clean
# input. Uses a throwaway git repo; fake values are assembled at run time so
# this file itself stays clean.
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
T="$(mktemp -d)"; trap 'rm -rf "$T"' EXIT
cd "$T" && git init -q . || exit 2
scan() { bash "$HERE/tools/hooks/secrets-scan.sh" --all >/dev/null 2>&1; }
rc=0
expect() { # name want(0|1)
  git add -A >/dev/null 2>&1
  scan; got=$?; [[ $got -eq 0 ]] && got=0 || got=1
  if [[ $got -ne $2 ]]; then echo "selftest: $1: expected rc=$2 got $got" >&2; rc=1; fi
}
printf 'hello = 1;\n' > ok.nix
printf 'a: ENC[AES256_GCM,data:x]\nsops:\n  age: []\n' > ok.sops.yaml
expect clean 0
printf 'x = "%s%s%s%s";\n' postgres ':' '//u:pass' '@host/db' > url.txt;           expect urlpass 1; rm url.txt
printf 'see %s%s%s\n' 'http://username' ':password' '@host:port/' > doc.txt;  expect placeholder 0; rm doc.txt
printf '%s %s%s%s\n' 'see http://username:password@h/ and' 'https://ad' 'min:s3cr3t' '@h/' > mix.txt; expect placeholder-plus-real 1; rm mix.txt
printf '%s %s %s\n' '-----BEGIN' 'RSA PRIVATE' 'KEY-----' > k.pem;     expect pem 1; rm k.pem
printf 'AGE-SECRET-%s\n' 'KEY-1ABC' > a.txt;                           expect age 1; rm a.txt
printf 'pw: hunter2\nsops:\n  age: []\n' > bad.sops.yaml;              expect plainsops 1; rm bad.sops.yaml
printf '{"pw": "hunter2", "sops": {"version": "3"}}\n' > bad.sops.json;      expect plainsops-json 1; rm bad.sops.json
printf '{"pw": "ENC[AES256_GCM,data:x]", "sops": {"version": "3"}}\n' > ok.sops.json; expect okjson 0; rm ok.sops.json
printf 'PW=hunter2\nsops_version=3.8\n' > bad.sops.env;                 expect plainsops-env 1; rm bad.sops.env
printf 'PW=ENC[AES256_GCM,data:x]\nsops_version=3.8\n' > ok.sops.env;   expect okenv 0; rm ok.sops.env
printf 't = "%s%s";\n' 'gh' 'p_abcdefghijklmnopqrstuvwxyz0123456789' > t.txt;   expect ghp 1; rm t.txt
printf 't = "%s%s";\n' 'github_' 'pat_abcdefghijklmnopqrstuvwxyz0123' > t.txt; expect ghpat 1; rm t.txt
printf 't = "%s%s";\n' 'tskey-' 'auth-kAbCdEf1234-0123456789abcdef' > t.txt;    expect tskey 1; rm t.txt
printf 't = "%s%s";\n' 'xo' 'xb-1234567890-abcdefghij' > t.txt;                 expect slack 1; rm t.txt
printf 't = "%s%s";\n' 'AK' 'IAABCDEFGHIJKLMNOP' > t.txt;                       expect aws 1; rm t.txt
sk="$(printf 'A%.0s' $(seq 1 86))"
printf 'publicKey = "cache-1:%s==";\n' "$sk" > t.txt;                    expect nixsecret 1; rm t.txt
printf 'c = "host=db user=u %s%s";\n' 'pass' 'word=s3cr3t' > t.txt;      expect connpw 1; rm t.txt
printf 'c = "host=db %s%s";\n' 'sslpass' 'word=s3cr3t' > t.txt;          expect connsslpw 1; rm t.txt
printf 'c = "host=db %s%s";\n' 'pass' 'word=$PGPASS' > t.txt;           expect connpw-var 0; rm t.txt
printf 'x\n' > a.dec.yaml;                                              expect dec 1; rm a.dec.yaml
printf 'x\n' > a.decrypted.yaml;                                        expect decrypted 1; rm a.decrypted.yaml
printf '{}\n' > t.tfstate;                                              expect tfstate 1; rm t.tfstate
printf '{}\n' > t.tfstate.backup;                                       expect tfstatebak 1; rm t.tfstate.backup
mkdir -p .fleet; echo x > .fleet/s;                                   expect forbidden 1; rm -r .fleet
expect clean-again 0
[[ $rc -eq 0 ]] && echo "secrets-scan selftest: ok" >&2
exit $rc
