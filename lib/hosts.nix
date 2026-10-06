# hosts: what the model says about each guest (or, with `site`, each node of
# the site) that names a `nixos.module`,
# as the module list its NixOS system is built from. Shared by mkSystems
# (plain nixosSystem) and mkHive (Colmena nodes). Evaluation only.
#
#   hosts {
#     fleet; estate | site; nixpkgs;   # exactly one of estate, site
#     modules;        # modules every guest gets
#     specialArgs;    # the tenant's app inputs (the caller passes them on)
#     inHive;         # true when Colmena declares `deployment` itself
#   } -> { <guest> = { modules; guest; }; }
#
# specialArgs is accepted (through `...`) but not read here: it reaches the
# modules through the caller (nixosSystem, or the hive's meta).
{
  fleet,
  estate ? null,
  site ? null,
  nixpkgs,
  modules,
  inHive,
  ...
}:
let
  inherit (nixpkgs) lib;
  isSite = site != null;
  siteNodes = fleet.sites.${site}.nodes;
  # A site's nodes that name a module, as guest-shaped records.
  nodes = lib.mapAttrs (
    name: n:
    n
    // {
      kind = "machine";
      on = "${site}/${name}";
      tags = [
        "machine"
        site
      ];
    }
  ) (lib.filterAttrs (_: n: n.nixos.module != null) siteNodes);
  guests =
    if (estate == null) == (site == null) then
      throw "hosts: pass exactly one of `estate` and `site`"
    else if isSite then
      nodes
    else
      fleet.guests.${estate};
  compute =
    if isSite then
      lib.mapAttrs (_: n: { internal_ip = n.address; }) siteNodes
    else
      lib.mapAttrs (_: g: { internal_ip = g.ipv4.lan or null; }) guests;
  inherit (fleet.operators) principals roles grants;
  keysOf = p: map (k: k.public) (lib.attrValues principals.${p}.keys.ssh);

  # A grant applies everywhere (where = null), or to the estates and regions
  # it names.
  applies =
    guest: g:
    g.where == null
    || (!isSite && lib.elem estate g.where.estates)
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
  siteDomains = lib.concatMap (n: n.domains) (lib.attrValues fleet.sites.${site}.networks);
  internalDomain =
    if isSite then
      (if siteDomains == [ ] then null else lib.head siteDomains)
    else if internal == [ ] then
      null
    else
      lib.head internal;
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
