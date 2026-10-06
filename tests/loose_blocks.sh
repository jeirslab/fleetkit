#!/usr/bin/env bash
# Loose-block inventory check. Fails when:
#  - modules/ contains an untyped type (anything|unspecified|raw|attrs) other
#    than the single one inside the looseBlock helper in modules/loose.nix;
#  - the number of looseBlock uses differs from fleet.report.looseBlocks;
#  - the docs/schema-todo.md loose-block table differs from
#    nix eval of fleet.report.looseBlocks.
# Run by tools/gates.sh as part of the lint gate.
set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT" || exit 2
WF="$ROOT/.fleet/wf"; mkdir -p "$WF"
flock "$WF/git.lock" git add -A >&2
fail=0

# strip comment-only lines, then find untyped types
untyped=$(rg -n --no-heading '(types\.(anything|unspecified|raw)\b|types\.attrs\b)' modules --glob '*.nix' | rg -v '^[^:]+:[0-9]+:\s*#' || true)
n_untyped=$(printf '%s' "$untyped" | rg -c . || true); n_untyped=${n_untyped:-0}
n_uses=$(rg -c '^\s+[a-z]+ = looseBlock "' modules/loose.nix || true); n_uses=${n_uses:-0}
mapfile -t evald < <(nix eval --json .#fleet.report.looseBlocks | python3 -c 'import json,sys; print("\n".join(sorted(json.load(sys.stdin))))')
if [[ "$n_untyped" != 1 || "$(printf '%s' "$untyped" | cut -d: -f1)" != modules/loose.nix ]]; then
  echo "loose: expected exactly one untyped type, in modules/loose.nix; found:" >&2
  printf '%s\n' "$untyped" >&2; fail=1
fi
if [[ "$n_uses" != "${#evald[@]}" ]]; then
  echo "loose: $n_uses looseBlock uses but ${#evald[@]} entries in fleet.report.looseBlocks" >&2; fail=1
fi
# docs table rows: first column in the "Loose blocks" section
mapfile -t docs < <(awk '/^## Loose blocks/{s=1;next} /^## /{s=0} s' docs/schema-todo.md \
  | rg -o '^\| `(fleet\.[^`]+)`' -r '$1' | sed 's/<e>/<estate>/g' | sort)
if [[ "$(printf '%s\n' "${docs[@]}")" != "$(printf '%s\n' "${evald[@]}")" ]]; then
  echo "loose: docs table differs from fleet.report.looseBlocks" >&2
  diff <(printf '%s\n' "${docs[@]}") <(printf '%s\n' "${evald[@]}") >&2; fail=1
fi
[[ $fail == 0 ]] && echo "loose: ok (${#evald[@]} blocks)" >&2
exit $fail
