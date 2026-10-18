# fleetkit: the fleet model's schema and checks. No data lives here; an estate
# repo supplies it. Pure: callers pass nixpkgs' `lib`.
#
#   mkFleet { lib; modules; tenants ? { }; }
#       -> the raw lib.evalModules result of modules/options.nix (schema),
#          `modules` (the lab's own data) and each tenant's slice
#          (<source>/fleet), plus `tenantViolations`
#   checked eval
#       -> eval.config, or a throw listing every failed assertion and every
#          tenant violation
#   fleet { lib; modules; tenants ? { }; }
#       -> the checked, JSON-serialisable config.fleet
#
# `tenants` maps an estate name to the source tree of the repo that declares
# it.
#
# The lab owns the hardware and the grants; a tenant declares what it wants
# inside its grant. That boundary is checked on the definitions themselves
# (which file set what): a tenant file may set only
#   fleet.estates.<tenant>.<tenantOwned>   fleet.guests.<tenant>
#   fleet.repos.<tenant>                   fleet.operators.principals.<new name>
# Everything else (sites, pools, placement, backends, zones, grants, ...) is
# the lab's, and a tenant definition of it is a violation.
#
# Reference validation is done with config.assertions (see modules/lib.nix);
# plain lib.evalModules does not enforce assertions, so every consumer goes
# through `checked`.
let
  tenantOwned = [
    "owner"
    "domains"
    "tailnets"
    "environments"
    "secrets"
    "observability"
    "git"
  ];

  mkFleet =
    {
      lib,
      tenants ? { },
      modules ? [ ],
    }:
    let
      sources = builtins.mapAttrs (_: toString) tenants;
      eval = lib.evalModules {
        modules = [ ../modules/options.nix ]
        ++ map (src: src + "/fleet") (builtins.attrValues sources)
        ++ modules;
      };

      optionDefs =
        path: node:
        if lib.isOption node then
          [
            {
              inherit path;
              defs = node.definitionsWithLocations;
            }
          ]
        else if builtins.isAttrs node then
          lib.concatLists (lib.mapAttrsToList (n: optionDefs (path ++ [ n ])) node)
        else
          [ ];
      allDefs = optionDefs [ ] (removeAttrs eval.options [ "_module" ]);

      from = src: d: lib.hasPrefix (src + "/") (toString d.file);
      fromAnyTenant = d: lib.any (src: from src d) (builtins.attrValues sources);
      only = names: value: builtins.isAttrs value && lib.all (n: lib.elem n names) (builtins.attrNames value);

      labPrincipals = lib.concatMap (
        o:
        lib.optionals
          (
            o.path == [
              "fleet"
              "operators"
              "principals"
            ]
          )
          (
            lib.concatMap (d: lib.optionals (!fromAnyTenant d) (builtins.attrNames d.value)) o.defs
          )
      ) allDefs;

      allowed =
        tenant: path: value:
        let
          p = lib.concatStringsSep "." path;
        in
        if p == "fleet.guests" || p == "fleet.repos" then
          only [ tenant ] value
        else if p == "fleet.estates" then
          only [ tenant ] value && lib.all (only tenantOwned) (builtins.attrValues value)
        else if p == "fleet.operators.principals" then
          builtins.isAttrs value && lib.all (n: !lib.elem n labPrincipals) (builtins.attrNames value)
        else
          false;

      # A tenant whose source matches no definition would make the boundary
      # check blind (a source path that is not where its files are read from).
      unseen = lib.mapAttrsToList (
        tenant: src: "tenant ${tenant}: no definition comes from its source ${src}; the tenant boundary cannot be checked"
      ) (lib.filterAttrs (_: src: !lib.any (o: lib.any (from src) o.defs) allDefs) sources);

      tenantViolations = unseen ++ lib.concatLists (
        lib.mapAttrsToList (
          tenant: src:
          lib.concatMap (
            o:
            lib.concatMap (
              d:
              lib.optional (from src d && !allowed tenant o.path d.value)
                "tenant ${tenant} (${lib.removePrefix (src + "/") (toString d.file)}) sets ${lib.concatStringsSep "." o.path} outside what a tenant owns"
            ) o.defs
          ) allDefs
        ) sources
      );
    in
    eval // { inherit tenantViolations; };

  checked =
    eval:
    let
      failed = builtins.filter (a: !a.assertion) eval.config.assertions;
      messages = map (a: "- ${a.message}") failed ++ map (m: "- ${m}") (eval.tenantViolations or [ ]);
    in
    if messages == [ ] then
      eval.config
    else
      throw ''
        fleet: ${toString (builtins.length messages)} failed assertion(s):
        ${builtins.concatStringsSep "\n" messages}'';
in
{
  inherit mkFleet checked tenantOwned;
  fleet = args: (checked (mkFleet args)).fleet;
}
