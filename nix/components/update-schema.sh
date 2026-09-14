#!/usr/bin/env bash
# Regenerate committed component interface schemas from the CURRENT tree.
#
# Run this after an INTENTIONAL interface change (a new/renamed option path,
# a type change), review the diff, and `git add` the result — the schema
# files ARE the interface contract. `nix flake check`'s component-* gates
# fail until the committed schema matches what the code declares.
#
# Modules only for now; tf/image families are added at M3/M4.
set -euo pipefail

root="$(git -C "$(dirname "$0")" rev-parse --show-toplevel)"
cd "$root"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

echo "evaluating module component interfaces…"
# getFlake reaches the flake's locked inputs so the eval matches the checks
# (--impure because the working tree is dirty during a refactor).
nix eval --impure --json --expr "
  let
    self = builtins.getFlake (toString $root);
    nixpkgs = self.inputs.nixpkgs;
    lib = nixpkgs.lib;
    pkgs = import nixpkgs { system = \"x86_64-linux\"; };
    shared = import $root/nix/components/eval.nix {
      inherit pkgs nixpkgs lib;
      sops-nix = self.inputs.sops-nix;
      disko = self.inputs.disko;
    };
    registry = import $root/nix/components/registry.nix { inherit lib; };
  in lib.listToAttrs (map (c: lib.nameValuePair c.name (shared.interfaceUnder c.src)) registry.modules)
" > "$tmp/all.json"

mkdir -p nix/components/schema/modules
count=0
for name in $(jq -r 'keys[]' "$tmp/all.json"); do
  jq --sort-keys --arg k "$name" '.[$k]' "$tmp/all.json" > "nix/components/schema/modules/$name.json"
  echo "  wrote nix/components/schema/modules/$name.json"
  count=$((count + 1))
done

echo "evaluating images family interface…"
mkdir -p nix/components/schema/images
nix eval --impure --json --expr "
  let imgs = (builtins.getFlake (toString $root)).lib.images;
  in { targetsData = imgs.targetsData; templatesData = imgs.templatesData; }
" | jq -S . > nix/components/schema/images/interface.json
echo "  wrote nix/components/schema/images/interface.json"

echo "regenerated $count module schema(s) + the images interface. Review the diff, then: git add nix/components/schema/"
