# Options for infra.network.dns (CoreDNS internal DNS server).
#
# Split out from the leaf's config (../default.nix) per the component model:
# these option paths are the leaf's EXPORTED INTERFACE, locked by the
# committed schema (nix/components/schema/modules/infra.network.dns.json).
# Refactor the config freely; changing an option path or type is a
# deliberate, schema-updating act.
{ config, lib, ... }:
let
  inherit (lib) mkEnableOption mkOption types;
in
{
  options.infra.network.dns = {
    enable = mkEnableOption "CoreDNS internal DNS server";

    domain = mkOption {
      type = types.nullOr types.str;
      default = config.fleet.settings.domain.internal;
      defaultText = lib.literalExpression "config.fleet.settings.domain.internal";
      description = "DNS zone to serve. Must be non-null when infra.network.dns is enabled (asserted).";
    };

    listenAddress = mkOption {
      type = types.str;
      default = "0.0.0.0";
      description = "Address CoreDNS listens on.";
    };

    port = mkOption {
      type = types.port;
      default = 53;
      description = "Port CoreDNS listens on.";
    };

    forwarders = mkOption {
      type = types.listOf types.str;
      default = config.fleet.self.settings.network.upstreamResolvers;
      defaultText = lib.literalExpression "config.fleet.self.settings.network.upstreamResolvers";
      description = "Upstream DNS servers for non-local queries. Defaults to the site-resolved fleet.settings.network.upstreamResolvers — a second site's resolver normally forwards to its own LAN gateway rather than the first site's (INFRA-307).";
    };

    records = mkOption {
      type = types.attrsOf types.str;
      default = {};
      example = lib.literalExpression ''{ app-db = "192.0.2.104"; grafana = "192.0.2.4"; }'';
      description = "Hostname → IP mapping for A records in the internal zone.";
    };

    publicDomain = mkOption {
      type = types.nullOr types.str;
      default = config.fleet.settings.domain.base;
      defaultText = lib.literalExpression "config.fleet.settings.domain.base";
      description = "Public DNS zone for internal (split-horizon) resolution of public names. Only forced when publicRecords is non-empty (asserted non-null then).";
    };

    publicRecords = mkOption {
      type = types.attrsOf types.str;
      default = {};
      example = lib.literalExpression ''{ vpn = "192.0.2.2"; }'';
      description = "Hostname → IP mapping for the public domain zone (internal resolution only).";
    };

    extraZones = mkOption {
      type = types.attrsOf (types.attrsOf types.str);
      default = {};
      example = lib.literalExpression ''
        { "example.xen" = { pbs = "192.0.2.99"; "platform.pve" = "192.0.2.98"; }; }
      '';
      description = ''
        Additional internal DNS zones beyond `domain`. Outer attrset
        key is the zone name; inner attrset is `record-name → IP`.
        Record names can be multi-label (e.g., "platform.pve",
        "nodes.btc.pve") to express hierarchy within the zone.
        Empty IPs are skipped, same as `records`.
      '';
    };
  };
}
