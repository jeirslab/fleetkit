# nix/components/eval.nix — the single NixOS eval of fleetkit's option trees.
#
# fleet.* (manifest schema) + infra.* (service modules) are evaluated ONCE
# here, and both the options documentation (../../docs) and the component
# module-schema checks (./checks.nix) read this same eval — so the option
# paths a check locks are exactly the ones the docs render. Options declared
# OUTSIDE this repo (NixOS, sops-nix, disko) are filtered by `isOurs`.
#
# This is deliberately the same eval docs/default.nix used to inline; it was
# lifted here verbatim (same modules, same host stub, same stateVersion) so
# the refactor is behaviour-preserving.
{ pkgs, nixpkgs, sops-nix, disko, lib ? pkgs.lib }:

let
  # nix/components/ → repo root is two levels up. A string, matched as a
  # prefix against option declaration paths.
  fleetkitRoot = toString ../..;

  eval = import (nixpkgs + "/nixos/lib/eval-config.nix") {
    system = "x86_64-linux";
    modules = [
      sops-nix.nixosModules.sops
      disko.nixosModules.disko
      ../modules
      ../fleet
      # Minimal host stub so the eval closes.
      {
        fileSystems."/" = { device = "none"; fsType = "tmpfs"; };
        boot.loader.grub.enable = false;
        system.stateVersion = "24.05";
      }
    ];
  };

  isOurs = opt:
    lib.any (d: lib.hasPrefix fleetkitRoot (toString d)) opt.declarations;

  # Every option fleetkit declares, flattened to a doc list — each entry has
  # `.name` (the full dotted option PATH), `.declarations`, `.type`, etc.
  # This is how a check gets real option paths: `builtins.attrNames` on a
  # module function does not work (paths only exist after evalModules).
  docList = lib.filter isOurs (lib.optionAttrSetToDocList eval.options);

  # The sorted option paths whose declaration file lives under `src` — the
  # per-component projection used to attribute an interface to a leaf.
  pathsUnder =
    src:
    let s = toString src;
    in lib.sort (a: b: a < b) (
      map (o: o.name) (
        lib.filter (o: lib.any (d: lib.hasPrefix s (toString d)) o.declarations) docList
      )
    );

  # Same projection, but carrying a coarse rendered type per path (so a
  # str→int break is caught while cosmetic changes are not).
  interfaceUnder =
    src:
    let s = toString src;
    in lib.listToAttrs (
      map (o: lib.nameValuePair o.name { inherit (o) type; }) (
        lib.filter (o: lib.any (d: lib.hasPrefix s (toString d)) o.declarations) docList
      )
    );
in
{
  inherit fleetkitRoot eval isOurs docList pathsUnder interfaceUnder;
}
