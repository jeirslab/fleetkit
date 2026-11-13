#!/usr/bin/env bash
# Experimental. Renders lib.mkPulumi for estate "mini" of tests/fixtures/tf-mini
# and runs `pulumi preview` on it: Pulumi parses the program, installs the
# bridged providers at the pinned versions, type-checks every property against
# them, decrypts the fixture's secret through the sops provider and plans the
# guests and the pool as creates.
#
# Not a gate: it needs the network (Pulumi plugins and the provider binaries
# from the OpenTofu registry). Nothing leaves this machine otherwise: the state
# is a file backend in a temp dir, the secret is a throwaway age key made here,
# and no Proxmox API is reached by a preview of creates (the endpoint is
# RFC 5737). Prints one JSON line: {"pulumi_preview":"pass"|"fail"}.
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
NIXPKGS="nixpkgs"

export PULUMI_HOME="${PULUMI_HOME:-$TMP/home}"
export PULUMI_BACKEND_URL="file://$TMP/state"
export PULUMI_CONFIG_PASSPHRASE="fleetkit-preview"
export PULUMI_SKIP_UPDATE_CHECK=1
mkdir -p "$TMP/state" "$TMP/p/secrets"

nix eval --impure --json "$ROOT#lib" --apply "l: l.mkPulumi {
  fleet = l.fleet { modules = [ $ROOT/tests/fixtures/tf-mini ]; };
  estate = \"mini\";
}" >"$TMP/p/Pulumi.yaml" || { echo '{"pulumi_preview":"fail"}'; exit 1; }

run() { nix shell "$NIXPKGS#pulumi-bin" "$NIXPKGS#sops" "$NIXPKGS#age" --command "$@"; }

# The fixture's secret file, encrypted to a key that exists only here.
run age-keygen -o "$TMP/age.key" 2>/dev/null
export SOPS_AGE_KEY_FILE="$TMP/age.key"
recipient="$(run age-keygen -y "$TMP/age.key")"
printf '{"integrations":{"proxmox":{"main":{"api_token":"root@pam!preview=00000000-0000-0000-0000-000000000000"}}}}' \
  >"$TMP/p/secrets/tf.json"
run sops --encrypt --age "$recipient" --in-place "$TMP/p/secrets/tf.json"

cd "$TMP/p" || exit 2
status=pass
run pulumi stack init preview --non-interactive >&2 || status=fail
if [[ $status == pass ]]; then
  run pulumi install >&2 || status=fail
fi
if [[ $status == pass ]] && ! run pulumi preview --non-interactive --diff >"$TMP/preview.out" 2>&1; then
  status=fail
fi
cat "$TMP/preview.out" >&2
[[ -n "${PREVIEW_KEEP:-}" ]] && cp "$TMP/preview.out" "$PREVIEW_KEEP"
# Every rendered resource is planned, and the token never shows in plain text.
if [[ $status == pass ]]; then
  for r in box machine tuned main provider-proxmox; do
    grep -q "$r" "$TMP/preview.out" || { echo "FAIL: $r not in the plan" >&2; status=fail; }
  done
  if grep -q '00000000-0000-0000-0000-000000000000' "$TMP/preview.out"; then
    echo "FAIL: the api token is shown in the plan" >&2
    status=fail
  fi
fi

printf '{"pulumi_preview":"%s"}\n' "$status"
[[ $status == pass ]]
