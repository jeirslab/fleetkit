# infra.network.dns — CoreDNS internal DNS server.
#
# Options are declared in ./options (component model — the schema-locked
# interface); this file consumes them. Importing the directory still yields
# the full module (options + config) by additive merge, so every existing
# `imports = [ …/infra/network/dns ]` is unaffected.
{ config, lib, ... }:
let
  inherit (lib) mkIf concatStringsSep mapAttrsToList;
  cfg = config.infra.network.dns;

  # Generate zone file content from records attrset (skip empty IPs)
  mkZoneRecords = records: concatStringsSep "\n" (lib.filter (s: s != "") (mapAttrsToList
    (name: ip: if ip != "" then "${name} IN A ${ip}" else "")
    records
  ));

  zoneRecords = mkZoneRecords cfg.records;

  mkZoneFile = zoneDomain: records: ''
    $ORIGIN ${zoneDomain}.
    $TTL 300

    @  IN SOA ns1.${zoneDomain}. admin.${zoneDomain}. (
         2024010101 ; serial
         3600       ; refresh
         900        ; retry
         604800     ; expire
         300        ; minimum
       )

       IN NS ns1.${zoneDomain}.

    ns1 IN A ${cfg.listenAddress}

    ${mkZoneRecords records}
  '';

  hasPublicRecords = cfg.publicRecords != {};

  # Generate a hosts-format file for the public domain overrides
  publicHostsFile = concatStringsSep "\n" (lib.filter (s: s != "") (mapAttrsToList
    (name: ip: if ip != "" then "${ip} ${name}.${cfg.publicDomain}" else "")
    cfg.publicRecords
  ));

  extraZoneBlocks = lib.concatStringsSep "\n" (lib.mapAttrsToList (zone: _: ''
    ${zone} {
      log
      errors
      file /etc/coredns/${zone}.zone ${zone}
    }
  '') cfg.extraZones);

  corefile = ''
    ${cfg.domain} {
      log
      errors
      prometheus :9153

      file /etc/coredns/${cfg.domain}.zone ${cfg.domain}
    }

    ${extraZoneBlocks}

    ${lib.optionalString hasPublicRecords ''
    ${cfg.publicDomain} {
      log
      errors

      hosts /etc/coredns/${cfg.publicDomain}.hosts {
        fallthrough
      }
      forward . ${concatStringsSep " " cfg.forwarders}
    }
    ''}

    . {
      forward . ${concatStringsSep " " cfg.forwarders}
      log
      errors
      cache 30
    }
  '';

  zoneFile = mkZoneFile cfg.domain cfg.records;
  extraZoneFiles = lib.mapAttrs mkZoneFile cfg.extraZones;
in
{
  imports = [ ./options ];

  config = mkIf cfg.enable {
    assertions = [
      {
        assertion = cfg.domain != null;
        message = "infra.network.dns.enable is set but infra.network.dns.domain is null — set fleet.settings.domain.internal (or infra.network.dns.domain explicitly).";
      }
      {
        assertion = !hasPublicRecords || cfg.publicDomain != null;
        message = "infra.network.dns.publicRecords is non-empty but infra.network.dns.publicDomain is null — set fleet.settings.domain.base (or infra.network.dns.publicDomain explicitly).";
      }
    ];

    services.coredns = {
      enable = true;
      config = corefile;
    };

    # INFRA-23: the Corefile references zone files via `file /etc/coredns/*.zone`,
    # so changing a record (zone-file content) leaves the systemd unit unchanged
    # — coredns keeps serving stale data until a manual `systemctl restart
    # coredns`. Key a restart to the zone/hosts content so record changes apply
    # on deploy (e.g. the .nodes chain-gateway zone, fleet A records).
    systemd.services.coredns.restartTriggers =
      [ zoneFile ]
      ++ lib.optional hasPublicRecords publicHostsFile
      ++ lib.attrValues extraZoneFiles;

    # Disable systemd-resolved stub listener — coredns owns port 53.
    services.resolved.settings.Resolve.DNSStubListener = "no";

    # Route internal-domain queries to local CoreDNS so step-ca ACME
    # challenges (and any local service) can resolve internal hostnames.
    networking.nameservers = lib.mkForce [ "127.0.0.1" ];
    networking.search = [ cfg.domain ];

    # Single environment.etc merge so the three sources (primary
    # zone, optional public-hosts file, extraZones zone files) don't
    # collide on the parent attr.
    environment.etc = lib.mkMerge [
      { "coredns/${cfg.domain}.zone".text = zoneFile; }
      (lib.mkIf hasPublicRecords {
        "coredns/${cfg.publicDomain}.hosts".text = publicHostsFile;
      })
      (lib.mapAttrs' (zone: contents: {
        name = "coredns/${zone}.zone";
        value = { text = contents; };
      }) extraZoneFiles)
    ];

    networking.firewall.allowedTCPPorts = [ cfg.port ];
    networking.firewall.allowedUDPPorts = [ cfg.port ];

    # Ship CoreDNS metrics to Prometheus via Alloy
    infra.observability.alloy.extraConfig = ''

      // ── CoreDNS metrics ─────────────────────────────
      prometheus.scrape "coredns" {
        targets = [
          {"__address__" = "127.0.0.1:9153"},
        ]
        forward_to = [prometheus.remote_write.default.receiver]
        scrape_interval = "15s"
        job_name = "coredns"
      }
    '';
  };
}
