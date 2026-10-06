# hosts: what the model says about each guest that names a `nixos.module`,
# as the module list its NixOS system is built from. Shared by mkSystems
# (plain nixosSystem) and mkHive (Colmena nodes). Evaluation only.
#
#   hosts {
#     fleet; estate; nixpkgs;
#     modules;        # modules every guest gets
#     specialArgs;    # the tenant's app inputs (the caller passes them on)
#     inHive;         # true when Colmena declares `deployment` itself
#   } -> { <guest> = { modules; guest; }; }
#
# specialArgs is accepted (through `...`) but not read here: it reaches the
# modules through the caller (nixosSystem, or the hive's meta).
{
  fleet,
  estate,
  nixpkgs,
  modules,
  inHive,
  ...
}:
let
  inherit (nixpkgs) lib;
  guests = fleet.guests.${estate};
  compute = lib.mapAttrs (_: g: { internal_ip = g.ipv4.lan or null; }) guests;
  inherit (fleet.operators) principals roles grants;
  keysOf = p: map (k: k.public) (lib.attrValues principals.${p}.keys.ssh);

  # A grant applies everywhere (where = null), or to the estates and regions
  # it names.
  applies =
    guest: g:
    g.where == null
    || lib.elem estate g.where.estates
    || lib.elem fleet.sites.${lib.head (lib.splitString "/" guest.on)}.region g.where.regions;
  grantsOn = guest: lib.filter (applies guest) (lib.attrValues grants);
  can = what: g: lib.elem what roles.${g.role}.can;

  # One account per granted role that can log in, with the keys of everyone
  # granted it.
  accountsOn =
    guest:
    lib.foldl' (
      acc: g:
      let
        r = roles.${g.role};
        prev = acc.${r.account} or {
          keys = [ ];
          sudo = false;
        };
      in
      acc
      // {
        ${r.account} = {
          keys = lib.unique (prev.keys ++ lib.concatMap keysOf g.principals);
          sudo = prev.sudo || can "sudo" g;
        };
      }
    ) { } (lib.filter (can "login") (grantsOn guest));
  rootKeysOn =
    guest:
    lib.unique (
      lib.concatMap (g: lib.concatMap keysOf g.principals) (
        lib.filter (g: can "sudo" g || can "deploy" g) (grantsOn guest)
      )
    );
  internal = lib.filter (z: fleet.zones.${z}.internal) fleet.estates.${estate}.domains;
  internalDomain = if internal == [ ] then null else lib.head internal;
in
lib.mapAttrs (name: guest: {
  inherit guest;
  modules = [
    (import ../nixos/base.nix {
      inherit
        guest
        name
        compute
        internalDomain
        ;
      accounts = accountsOn guest;
      rootKeys = rootKeysOn guest;
      declareDeployment = !inHive;
    })
  ]
  ++ modules
  ++ [ guest.nixos.module ];
}) (lib.filterAttrs (_: g: g.nixos.module != null) guests)
