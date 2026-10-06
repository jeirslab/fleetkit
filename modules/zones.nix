# fleet.zones: DNS zones, keyed by FQDN; id = the FQDN.
#
# `host` is a plain enum today (the DNS host), not a reference.
{ lib, config, ... }:
let
  inherit (lib) types mkOption;
  h = import ./lib.nix { inherit lib; };
  ids = config.fleet.report.ids;

  serverType = types.submodule {
    options = {
      guest = mkOption {
        type = types.str;
        description = "Guest id running the ACME server.";
      };
      network = mkOption {
        type = types.str;
        description = "Network id the ACME server is reached on.";
      };
      port = mkOption {
        type = types.port;
        description = "ACME server port.";
      };
      provisioner = mkOption {
        type = types.str;
        description = "ACME provisioner name.";
      };
    };
  };

  acmeType = types.submodule {
    options = {
      method = mkOption {
        type = types.nullOr (
          types.enum [
            "dns-01"
            "http-01"
            "internal"
          ]
        );
        default = null;
      };
      email = mkOption {
        type = types.nullOr types.str;
        default = null;
      };
      server = mkOption {
        type = types.nullOr serverType;
        default = null;
      };
    };
  };

  zoneType = types.submodule (
    { name, ... }:
    {
      options = {
        id = h.mkId name;
        owner = mkOption {
          type = types.str;
          description = "Estate id owning the zone.";
        };
        host = mkOption {
          type = types.nullOr (types.enum [ "cloudflare" ]);
          default = null;
          description = "DNS host (plain string today, not a reference).";
        };
        account = mkOption {
          type = types.nullOr types.str;
          default = null;
          description = "Account id (<provider>/<name>) used to manage the zone.";
        };
        internal = mkOption {
          type = types.bool;
          default = false;
        };
        acme = mkOption {
          type = types.nullOr acmeType;
          default = null;
        };
      };
    }
  );

  zoneAssertions =
    zname: zone:
    let
      where = "fleet.zones.\"${zname}\"";
      inherit (zone) acme;
      server = if acme == null then null else acme.server;
      method = if acme == null then null else acme.method;
      accountProvider = lib.head (lib.splitString "/" zone.account);
    in
    [
      (h.refAssertion {
        where = "${where}.owner";
        kind = "estate";
        ids = ids.estate;
      } zone.owner)
    ]
    ++ h.optionalRefAssertions {
      where = "${where}.account";
      kind = "account";
      ids = ids.account;
    } zone.account
    ++ lib.optionals (server != null) [
      (h.refAssertion {
        where = "${where}.acme.server.guest";
        kind = "guest";
        ids = ids.guest;
      } server.guest)
      (h.refAssertion {
        where = "${where}.acme.server.network";
        kind = "network";
        ids = ids.network;
      } server.network)
    ]
    ++ [
      {
        assertion = method != "internal" || server != null;
        message = "${where}.acme: method \"internal\" requires acme.server";
      }
      {
        assertion = method != "dns-01" || zone.host != null || zone.account != null;
        message = "${where}.acme: method \"dns-01\" requires host or account";
      }
      {
        assertion = zone.account == null || zone.host == null || accountProvider == zone.host;
        message = "${where}: account \"${toString zone.account}\" provider does not match host \"${toString zone.host}\"";
      }
    ];
in
{
  options.fleet.zones = mkOption {
    type = types.attrsOf zoneType;
    default = { };
  };

  config.assertions = lib.concatLists (lib.mapAttrsToList zoneAssertions config.fleet.zones);
}
