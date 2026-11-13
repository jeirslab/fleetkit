# The few fleet options modules/secrets.nix reads, for a registry evaluated on
# its own (lib.evalRegistry, a data repository's checks): the guests and site
# nodes come from fleet.secretHosts, and assertions are collected. Inside a
# fleet model (fleetkit's mkFleet) this module is not used; the model has them.
{ lib, config, ... }:
let
  inherit (lib) types mkOption;
  hosts = config.fleet.secretHosts;
  idsOfKind = kind: lib.attrNames (lib.filterAttrs (_: h: h.kind == kind) hosts);
  split =
    id:
    let
      p = lib.splitString "/" id;
    in
    {
      parent = lib.head p;
      name = lib.concatStringsSep "/" (lib.tail p);
    };
  group =
    kind:
    lib.foldl' (
      acc: id:
      let
        p = split id;
        h = hosts.${id};
      in
      acc
      // {
        ${p.parent} = (acc.${p.parent} or { }) // {
          ${p.name} = {
            inherit id;
            hostKeys = { inherit (h) age ed25519; };
          };
        };
      }
    ) { } (idsOfKind kind);
in
{
  options = {
    assertions = mkOption {
      type = types.listOf types.unspecified;
      default = [ ];
      internal = true;
    };
    fleet = {
      guests = mkOption {
        type = types.raw;
        readOnly = true;
        default = group "guest";
        description = "Derived from fleet.secretHosts (kind = guest): <estate>.<name> = { id; hostKeys; }.";
      };
      sites = mkOption {
        type = types.raw;
        readOnly = true;
        default = lib.mapAttrs (_: nodes: { inherit nodes; }) (group "node");
        description = "Derived from fleet.secretHosts (kind = node): <site>.nodes.<name> = { id; hostKeys; }.";
      };
      report.ids = mkOption {
        type = types.raw;
        readOnly = true;
        default = {
          guest = idsOfKind "guest";
          node = idsOfKind "node";
        };
      };
    };
  };
}
