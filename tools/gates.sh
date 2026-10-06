#!/usr/bin/env bash
# fleetkit gates: parse, eval, lint, fidelity. Evaluation only; nothing is
# built. Human detail goes to stderr; stdout carries exactly one JSON line:
#   {"parse":"pass|fail","eval":"pass|fail","lint":"pass|fail","fidelity":"pass|fail","ok":true|false}
# Exit 0 iff ok. The schema is checked against real data by the gates of the
# estate repo that consumes it (negative cases, parity).
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WF="$ROOT/.fleet/wf"
NIXPKGS="github:NixOS/nixpkgs/b5aa0fbd538984f6e3d201be0005b4463d8b09f8"
mkdir -p "$WF"
cd "$ROOT" || exit 2

log() { printf '%s\n' "$*" >&2; }

# Flake evaluation only sees tracked files.
flock "$WF/git.lock" git -C "$ROOT" add -A >&2 || log "gates: git add failed"

mapfile -t NIXFILES < <(git -C "$ROOT" ls-files '*.nix')

parse=pass
for f in "${NIXFILES[@]}"; do
  if ! nix-instantiate --parse "$f" >/dev/null 2>"$WF/parse.err"; then
    parse=fail
    log "parse: FAIL $f"
    cat "$WF/parse.err" >&2
  fi
done
log "parse: $parse (${#NIXFILES[@]} files)"

# The schema with no data must evaluate and describe itself, and the
# deploy entry points must work on the fixture.
eval_=pass
if ! nix eval --json .#fleet.report >/dev/null 2>"$WF/eval.err"; then
  eval_=fail
  log "eval: FAIL"
  tail -n 40 "$WF/eval.err" >&2
fi
# mkHive and mkSystems on the hive-mini fixture (evaluation only).
if ! bash tests/hive.sh >&2; then
  eval_=fail
  log "eval: tests/hive.sh FAIL"
fi
log "eval: $eval_"

lint=pass
if ! nix shell "$NIXPKGS#statix" "$NIXPKGS#deadnix" -c bash -c '
  rc=0
  statix check . >&2 || rc=1
  deadnix --fail "$@" >&2 || rc=1
  exit $rc
' lint "${NIXFILES[@]}"; then
  lint=fail
fi
for t in tests/loose_blocks.sh tests/sops_config.sh "tools/hooks/secrets-scan.sh --all" tests/secrets_scan_selftest.sh; do
  # shellcheck disable=SC2086
  if ! bash $t >&2; then
    lint=fail
    log "lint: $t FAIL"
  fi
done
log "lint: $lint"

# Every guest option path is mapped to a provider argument that exists in the
# pinned bpg/proxmox schema (docs/guest-provider-map.md), and so is the
# terraform rendered from the fixture (tests/terraform.sh), and the GitHub
# terraform against the pinned integrations/github schema (tests/github.sh).
fidelity=pass
python3 tests/guest_fidelity.py --gate >&2 || fidelity=fail
bash tests/terraform.sh >&2 || fidelity=fail
bash tests/github.sh >&2 || fidelity=fail
log "fidelity: $fidelity"

ok=false
[[ $parse == pass && $eval_ == pass && $lint == pass && $fidelity == pass ]] && ok=true
printf '{"parse":"%s","eval":"%s","lint":"%s","fidelity":"%s","ok":%s}\n' "$parse" "$eval_" "$lint" "$fidelity" "$ok"
[[ $ok == true ]]
