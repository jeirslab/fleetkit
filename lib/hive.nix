# mkHive: a Colmena hive for an estate, one node per guest that names a
# `nixos.module`. Evaluation only; nothing here builds or deploys.
#
#   mkHive {
#     fleet;                  # the checked model (config.fleet)
#     estate;                 # estate name: that estate's guests
#     site;                   # or a site name: that site's nodes (exactly one of the two)
#     nixpkgs;                # the nixpkgs the estate's modules are written for
#     modules ? [ ];          # modules every node gets
#     specialArgs ? { };      # the tenant's app inputs
#     network ? "lan";        # the network whose address is the deploy target
#     system ? "x86_64-linux";
#   } -> { meta; <guest> = <node module>; }
#
# With `site`, the nodes of that site that name a module: targetHost is the
# node's address, targetUser root, tags [ "machine" <site> ].
#
# deployment.targetHost is the guest's address on `network`, targetUser is
# the estate's colmena.targetUser (default root), tags are the guest's tags
# plus its kind and the estate. A guest with no address on `network` throws.
{
  fleet,
  estate ? null,
  site ? null,
  nixpkgs,
  modules ? [ ],
  specialArgs ? { },
  network ? "lan",
  system ? "x86_64-linux",
}:
let
  inherit (nixpkgs) lib;
  hosts = import ./hosts.nix {
    inherit
      fleet
      estate
      site
      nixpkgs
      modules
      specialArgs
      ;
    inHive = true;
  };
  colmena = if site != null then null else fleet.estates.${estate}.colmena or null;
  targetUser = if colmena == null then "root" else colmena.targetUser or "root";
  targetHostOf =
    name: guest:
    let
      addr = if site != null then guest.address else guest.ipv4.${network} or null;
    in
    if addr == null then
      throw "mkHive: guest '${name}' of estate '${estate}' has no address on network '${network}'"
    else
      addr;
in
{
  meta = {
    nixpkgs = import nixpkgs { inherit system; };
    inherit specialArgs;
  };
}
// lib.mapAttrs (name: h: {
  imports = h.modules;
  deployment = {
    targetHost = targetHostOf name h.guest;
    inherit targetUser;
    tags =
      if site != null then
        h.guest.tags
      else
        (if h.guest.tags or null == null then [ ] else h.guest.tags)
        ++ [
          h.guest.kind
          estate
        ];
  };
}) hosts
