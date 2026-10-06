# mkSystems: one NixOS system per guest of an estate that names a
# `nixos.module`. Evaluation only; nothing here builds or deploys.
#
#   mkSystems {
#     fleet;                # the checked model (config.fleet)
#     estate;               # estate name
#     nixpkgs;              # the nixpkgs the estate's modules are written for
#     modules ? [ ];        # modules every system gets (sops-nix, the tenant's globals, ...)
#     specialArgs ? { };    # the tenant's app inputs
#   } -> { <guest> = <nixosSystem>; }
{
  fleet,
  estate,
  nixpkgs,
  modules ? [ ],
  specialArgs ? { },
}:
let
  inherit (nixpkgs) lib;
  hosts = import ./hosts.nix {
    inherit
      fleet
      estate
      nixpkgs
      modules
      specialArgs
      ;
    inHive = false;
  };
in
lib.mapAttrs (
  _: h:
  lib.nixosSystem {
    inherit specialArgs;
    inherit (h) modules;
  }
) hosts
