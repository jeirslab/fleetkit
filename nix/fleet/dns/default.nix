{ config, lib, ... }:

# DNS record derivation from fleet.compute — framework machinery only.
# The DATA (which alias points at which host, static pins, public
# overrides) is supplied by the consumer repo via the options below;
# fleetkit derives the final record sets:
#
#   fleet.dnsRecords       = auto per-host A records (internal_ip)
#                            // resolved service aliases
#                            // fleet.dnsStaticRecords
#   fleet.publicDnsRecords = resolved service aliases
#                            // fleet.dnsPublicOverrides
#
# Consumer paths: an internal DNS server module (e.g. CoreDNS) reads
# `dnsRecords` / `publicDnsRecords`; per-host modules get the same via
# mkHosts helpers (_module.args.helpers).

let
  cfg = config.fleet;

  # eth1 / internal_ip preferred, fall back to eth0.
  _internalIp = h:
    if h ? properties then (h.properties.ipv4 or {}).eth1 or ""
    else h.internal_ip or h.ip or "";

  # Externally-provisioned hosts (provisioning = "external") are
  # excluded: their IPs live outside the fleet network, so a record
  # would resolve but never route for fleet clients.
  fleetHosts = lib.filterAttrs (_: h: (h.provisioning or "managed") == "managed") cfg.hostsJson;

  autoRecords = lib.mapAttrs (_: _internalIp) fleetHosts;

  # The same auto records, split by the provider instance that provisions
  # each host. See the option below for why this is not just a convenience.
  providerInstances = lib.unique
    (lib.filter (s: s != "")
      (lib.mapAttrsToList (_: h: h.provider_instance or "") fleetHosts));

  recordsByProvider = lib.genAttrs providerInstances (inst:
    lib.mapAttrs (_: _internalIp)
      (lib.filterAttrs (_: h: (h.provider_instance or "") == inst) fleetHosts));

  # service-name → fleet.compute key (resolves to that host's internal IP)
  resolvedAliases =
    lib.mapAttrs (_: hostName: _internalIp (cfg.hostsJson.${hostName} or {}))
      cfg.serviceAliasMap;
in
{
  # ── Consumer-supplied data ──────────────────────────────────────
  options.fleet.serviceAliasMap = lib.mkOption {
    type = lib.types.attrsOf lib.types.str;
    default = {};
    example = lib.literalExpression ''{ grafana = "observe"; wiki = "docs-host"; }'';
    description = ''
      subdomain → fleet.compute key. Each alias resolves to that host's
      internal IP in both dnsRecords and publicDnsRecords. Single
      source of truth for "which fleet host hosts which service" —
      also readable by external-DNS zone resources so public and
      internal record sets stay consistent.
    '';
  };

  options.fleet.dnsStaticRecords = lib.mkOption {
    type = lib.types.attrsOf lib.types.str;
    default = {};
    example = lib.literalExpression ''{ ntp = "192.0.2.2"; ca = "192.0.2.2"; }'';
    description = ''
      name → literal IP, merged into dnsRecords last. For service
      aliases pinned to an address rather than a fleet host (edge
      services like ca/ntp/dns on the ingress box).
    '';
  };

  options.fleet.dnsPublicOverrides = lib.mkOption {
    type = lib.types.attrsOf lib.types.str;
    default = {};
    example = lib.literalExpression ''{ vpn = "192.0.2.2"; }'';
    description = ''
      name → literal IP, merged into publicDnsRecords last. Split-DNS
      overrides for names whose public zone answer (WAN IP) is not
      reachable from inside the fleet (no NAT hairpin) — point them at
      the internal ingress instead.
    '';
  };

  # ── Derived exports ─────────────────────────────────────────────
  options.fleet.dnsRecords = lib.mkOption {
    type = lib.types.attrsOf lib.types.str;
    default = {};
    internal = true;
    description = "Internal DNS A records (auto + service aliases + static).";
  };

  options.fleet.dnsRecordsByProvider = lib.mkOption {
    type = lib.types.attrsOf (lib.types.attrsOf lib.types.str);
    default = {};
    internal = true;
    description = ''
      Auto per-host A records grouped by provider instance — the same
      derivation as dnsRecords, partitioned rather than flattened, and
      without service aliases or static records (those are fleet-wide by
      construction and have no provider to attribute them to).

      dnsRecords spans the whole manifest, which is correct for a
      single-site fleet and wrong the moment a second site exists: a
      resolver that answers with an address its clients cannot route to
      turns a fast NXDOMAIN into a connection that hangs and then fails.
      Serving only the partition a resolver can actually reach is the
      interim answer until the two sites route to each other, at which
      point the consumer widens back to dnsRecords by deleting one
      reference.

      Provider instance is the available axis, not a perfect stand-in for
      "site": one site can span several instances (a hypervisor plus its
      appliance layer), in which case the consumer merges the partitions
      it wants. It does hold exactly when a site is one hypervisor.
    '';
  };

  options.fleet.publicDnsRecords = lib.mkOption {
    type = lib.types.attrsOf lib.types.str;
    default = {};
    internal = true;
    description = "Split-DNS records for the public base domain (aliases + overrides).";
  };

  config.fleet.dnsRecords = autoRecords // resolvedAliases // cfg.dnsStaticRecords;
  config.fleet.dnsRecordsByProvider = recordsByProvider;
  config.fleet.publicDnsRecords = resolvedAliases // cfg.dnsPublicOverrides;
}
