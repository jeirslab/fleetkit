# nix/components/checks.nix — component interface-schema gates.
#
# For each registered component, a check that the leaf's EXPORTED INTERFACE
# still matches its committed schema — for a module, the set of option paths
# plus a coarse type per path — failing on drift WITHOUT byte-diffing build
# output, so internals stay free. Flattened and namespaced into
# checks.<system> as `component-<family>-<name>`, a keyspace disjoint from
# nix/checks.nix. tf and image family gates join at M3/M4.
{ nixpkgs, sops-nix, disko, nixos-generators }:

let
  pkgs = import nixpkgs { system = "x86_64-linux"; };
  lib = nixpkgs.lib;

  registry = import ./registry.nix { inherit lib; };
  shared = import ./eval.nix { inherit pkgs nixpkgs sops-nix disko lib; };

  slugOf = name: lib.replaceStrings [ "." ] [ "-" ] name;

  # Module family: lock the option-path + coarse-type surface the leaf
  # declares (computed from the one shared eval), diffed against the
  # committed golden. A rename/removal/type-change fails; an internal
  # refactor that keeps the same paths and types passes.
  moduleCheck =
    c:
    let
      committed = ./schema/modules + "/${c.name}.json";
    in
    if !(builtins.pathExists committed) then
      pkgs.runCommand "component-module-${slugOf c.name}" { } ''
        echo "no committed schema for ${c.name}: run nix/components/update-schema.sh"; exit 1
      ''
    else
      pkgs.runCommand "component-module-${slugOf c.name}"
        {
          nativeBuildInputs = [ pkgs.jq pkgs.diffutils ];
          current = builtins.toJSON (shared.interfaceUnder c.src);
          inherit committed;
          passAsFile = [ "current" ];
        }
        ''
          if ! diff -u <(jq -S . "$committed") <(jq -S . "$currentPath"); then
            echo "interface of ${c.name} drifted from its schema — if intended, run nix/components/update-schema.sh"
            exit 1
          fi
          touch $out
        '';
  # tf family: existence-gate the registered emitters. The RENDERED resource
  # surface is locked by compute-surface-golden (nix/checks.nix); this catches
  # a deleted/renamed emitter source. A per-emitter structural surface gate is
  # a follow-up (needs broader fixtures + resource-type→emitter attribution).
  tfRegistered =
    let missing = lib.filter (c: !(builtins.pathExists c.src)) registry.tf;
    in
    pkgs.runCommand "component-tf-registered" { } (
      if missing == [ ] then
        "touch $out"
      else
        ''echo "missing tf emitter source(s): ${lib.concatMapStringsSep ", " (c: c.name) missing}" >&2; exit 1''
    );

  # images family: lock the exported interface as a whole — the target table
  # (targetsData) + the ADR-0003 template refs (templatesData). No per-target
  # dirs exist (targets share the bootstrap + a platform module), so the family
  # is one gate. Cross-consistency with the tf template constants is deferred to
  # the flip (the naming differs by design until then).
  imageInterface =
    let
      images = import ../images/deployer/lib { inherit nixpkgs nixos-generators; };
      committed = ./schema/images/interface.json;
    in
    if !(builtins.pathExists committed) then
      pkgs.runCommand "component-image-interface" { } ''
        echo "no committed images interface: run nix/components/update-schema.sh"; exit 1
      ''
    else
      pkgs.runCommand "component-image-interface"
        {
          nativeBuildInputs = [ pkgs.jq pkgs.diffutils ];
          current = builtins.toJSON { inherit (images) targetsData templatesData; };
          inherit committed;
          passAsFile = [ "current" ];
        }
        ''
          if ! diff -u <(jq -S . "$committed") <(jq -S . "$currentPath"); then
            echo "images family interface drifted — if intended, run nix/components/update-schema.sh"
            exit 1
          fi
          touch $out
        '';
in
(lib.listToAttrs (
  map (c: lib.nameValuePair "component-module-${slugOf c.name}" (moduleCheck c)) registry.modules
))
// lib.optionalAttrs (registry.tf != [ ]) { component-tf-registered = tfRegistered; }
// { component-image-interface = imageInterface; }
