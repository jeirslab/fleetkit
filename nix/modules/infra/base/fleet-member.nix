{ config, lib, ... }:

# Fleet-member layer — the fleet-aware layer above core.
#
# Splits cleanly: ../core/ is "any NixOS host with the right substrate
# could boot from this", ./ is "fleet member, talks to fleet DNS,
# trusts fleet CA, ships logs to fleet observability". Bootstrap
# templates intentionally skip this module — a fresh VM has no
# business looking for fleet DNS before its first Colmena push.
#
# Contents:
#   * Fleet-aware networking (singleInterface + internal/external IP
#     options, second-NIC static on vmbr1, fleet DNS, internal domain)
#   * Internal CA trust
#   * Builder binary cache substituter wiring
#   * Alloy telemetry agent enabled
#   * SOPS scaffold for the github machine-user token + nix
#     access-tokens.conf
#   * nixpkgs unfree allow-list

let
  inherit (lib) mkOption mkIf types;

  hostsLib = import ../../../lib/inventory.nix { hosts = config.fleet.hostsJson; };
  cache = hostsLib.builderCache;

  sopsLib = import ../../../lib/sops.nix { inherit lib; };
  p = config.sops.placeholder;

  netCfg = config.infra.networking;

  # Site-resolved settings for THIS host (INFRA-307). Identical to the
  # estate-wide values until the consumer declares `fleet.sites`, and the
  # reason to read them here rather than `fleet.network.*` directly is that
  # every value below names an address: a gateway, a resolver, a cache. A
  # second site inherits none of those correctly, and the failures are
  # quiet — an unroutable substituter costs a connect timeout per nix
  # operation, and an off-link gateway holds networkd at SETUP
  # `configuring` until wait-online times out and colmena calls the whole
  # activation failed.
  selfNet = config.fleet.self.settings.network;
  selfCache = config.fleet.self.settings.cache;
  selfCa = config.fleet.self.settings.internalCa;
in
{
  # ── Fleet networking options ───────────────────────────────────
  # These mirror the v1 core.nix schema verbatim so existing host
  # configs that set `infra.networking.singleInterface = true;` etc.
  # don't need touching.
  options.infra.githubAccessToken = mkOption {
    type = types.bool;
    default = config.fleet.settings.githubAccessTokens;
    defaultText = lib.literalExpression "config.fleet.settings.githubAccessTokens";
    description = "Provision the GitHub machine-user token (SOPS integrations/github/machine_user_token) into nix access-tokens on THIS host. Needed only where nix itself fetches private GitHub repos: a builder, or a machine deploys are run from. Defaults to the fleet-wide fleet.settings.githubAccessTokens so a fleet can flip the default off and opt individual hosts in.";
  };

  options.infra.networking = {
    internalIp = mkOption {
      type = types.str;
      default = "";
      example = "192.0.2.104";
      description = ''
        Static IP for the internal service network (vmbr1). Set
        automatically from hosts.json by nix/lib/default.nix.
      '';
    };
    internalGateway = mkOption {
      type = types.str;
      default = "";
      example = "192.0.2.1";
      description = "Gateway for internal network. Only set on netgate.";
    };
    singleInterface = mkOption {
      type = types.bool;
      default = false;
      description = ''
        When true, host has one NIC. Default placement is eth0 on vmbr1
        with the static `internalIp` + internal gateway. If `externalIp`
        is also set (and `internalIp` is empty), eth0 is placed on
        vmbr0 with the static `externalIp` + LAN gateway instead
        (vmbr0-only hosts, e.g. a public landing page).
      '';
    };
    externalIp = mkOption {
      type = types.str;
      default = "";
      example = "198.51.100.20";
      description = ''
        LAN-side IPv4 address for vmbr0-only single-NIC hosts.
        Auto-wired from the fleet's `ip` field by nix/lib/default.nix
        when `internalIp` is unset. Ignored unless `singleInterface` is
        true and `internalIp` is empty.
      '';
    };
  };

  config = {
    # ── Fleet networking implementation ───────────────────────────
    # DNS: the running link uses fleet DNS (CoreDNS) ONLY —
    # config.fleet.network.internal_resolvers, NOT dns_servers. A public
    # resolver must never sit on this link: systemd-resolved's per-link
    # failover is sticky, so a single fleet-DNS blip flips resolved to the
    # public server and it never flips back — and when the fleet's public
    # base domain is a real public zone, the public answer (the WAN IP)
    # wins for internal names, which many routers can't hairpin from the
    # LAN. That took ~15 tailnet nodes offline on 2026-06-19 (INFRA-107).
    # External names are forwarded by fleet DNS; a fleet-DNS outage falls
    # back (non-stickily) to systemd-resolved's built-in global FallbackDNS
    # for external names only, then self-heals. Fresh-host bootstrap keeps
    # its public fallback via dns_servers in the create-time dnsConfig
    # (nix/lib/tf/proxmox.nix).
    #
    # mkForce overrides the core's plain DHCP eth0 with the fleet-aware
    # branch that picks between vmbr0-only and vmbr1-static.
    systemd.network.networks."10-eth0" = lib.mkForce (
      if netCfg.singleInterface && netCfg.externalIp != "" then {
        # Single-NIC, LAN-only host (vmbr0). Static external IP + LAN
        # gateway via the LAN router.
        matchConfig.Name = "eth0";
        addresses = [{ Address = "${netCfg.externalIp}/${toString config.fleet.network.lan_prefix_len}"; }];
        routes = lib.optional (config.fleet.network.lan_gateway != null)
          { Gateway = config.fleet.network.lan_gateway; };
        # Fleet-DNS-only (no public resolver on this link) — see INFRA-107.
        networkConfig.DNS = selfNet.internalResolvers;
        # Routing domains pinned to this link so systemd-resolved sends
        # every fleet-served zone at fleet DNS — including public zones the
        # fleet answers with split-DNS INTERNAL IPs (fleet hosts hit the
        # internal ingress directly; external clients hit the WAN IP via
        # public DNS). The split-DNS answer is only correct from fleet DNS,
        # which is why this link must not carry a public resolver (INFRA-107).
        networkConfig.Domains = config.fleet.network.search_domains;
      }
      else if netCfg.singleInterface then {
        # Single-NIC, vmbr1-only host. Default route via the `router`
        # LXC (ADR-021 Phase 1.b). DNS also via router (CoreDNS moved
        # off netgate, Phase 3.c). Both pulled from fleet.network.* so
        # a future move only requires editing the manifest.
        matchConfig.Name = "eth0";
        addresses = [{ Address = "${netCfg.internalIp}/${toString config.fleet.network.internal_prefix_len}"; }];
        routes = lib.optional (selfNet.gateway != null)
          { Gateway = selfNet.gateway; };
        # Fleet-DNS-only (no public resolver on this link) — see INFRA-107.
        networkConfig.DNS = selfNet.internalResolvers;
        # Routing domains pinned to this link — same split-DNS rationale as
        # the vmbr0 branch above (INFRA-107).
        networkConfig.Domains = config.fleet.network.search_domains;
      }
      else {
        # Dual-NIC: eth0 = DHCP on vmbr0 for WAN egress, eth1 (below)
        # = static on vmbr1 for fleet-internal traffic.
        matchConfig.Name = "eth0";
        networkConfig.DHCP = "ipv4";
        dhcpV4Config = { UseDNS = true; UseHostname = false; };
      }
    );

    # ── Bootstrap-artifact mask (INFRA-42 discovery) ──────────────
    # The launcher's container bootstrap drops a plain
    # 00-bootstrap-eth0.network (Address+Gateway only, no DNS) so a
    # fresh CT is reachable before its first colmena deploy. networkd
    # matches the first file alphabetically, so if that file survives
    # the first deploy it shadows 10-eth0 above and the host runs
    # without DNS/search domains (observed on pgweb + grafana,
    # 2026-06-12). Declaring the same path as a zero-length managed
    # file replaces the leftover and networkd treats empty configs as
    # masked — 10-eth0 wins again. No-op on hosts that never had the
    # artifact.
    environment.etc."systemd/network/00-bootstrap-eth0.network".text = "";
    systemd.services.systemd-networkd.restartTriggers = [
      config.environment.etc."systemd/network/00-bootstrap-eth0.network".source
    ];

    systemd.network.networks."10-eth1" = mkIf (!netCfg.singleInterface && netCfg.internalIp != "") ({
      matchConfig.Name = "eth1";
      addresses = [{ Address = "${netCfg.internalIp}/${toString config.fleet.network.internal_prefix_len}"; }];
      # Fleet-DNS-only on the internal link (no public resolver) — INFRA-107.
      networkConfig.DNS = selfNet.internalResolvers;
      networkConfig.Domains = lib.optional (config.fleet.network.dns_domain != null)
        config.fleet.network.dns_domain;
    } // lib.optionalAttrs (netCfg.internalGateway != "") {
      routes = [{ Gateway = netCfg.internalGateway; }];
    });

    # ── Internal CA trust ─────────────────────────────────────────
    # Site-resolved (INFRA-307): each step-ca mints its own root, so a host
    # must trust the root of the CA it actually orders from. Reading the
    # estate-wide value here would hand a second site's hosts the first
    # site's root — which fails in the direction that is hardest to read,
    # since the ACME order succeeds and only the verification afterwards
    # does not. A site with no CA of its own resolves back to the
    # estate-wide value, and to null if there is none.
    security.pki.certificateFiles =
      lib.optional (selfCa.certFile != null) selfCa.certFile;

    # ── Builder binary cache ──────────────────────────────────────
    # Automatically configured when builder IP + public key are
    # available. Fleet is the source of truth for all host IPs.
    # In-fleet caches come from fleet.settings.cache — explicit
    # parameters, not file-sniffing (the old builderCache helper read a
    # pub-key file from a repo-relative path and silently disabled the
    # cache when it moved) — resolved per site, so a host only ever gets
    # caches its own site can route to (INFRA-307).
    nix.settings.substituters = lib.mkAfter ([
      "https://nix-community.cachix.org"
    ] ++ selfCache.substituters);
    nix.settings.trusted-public-keys = lib.mkAfter ([
      "nix-community.cachix.org-1:mB9FSh9qf2dCimDSUo8Zy7bkq5CX+/rkCWyvRCYg3Fs="
    ] ++ selfCache.trustedPublicKeys);

    # ── GitHub machine-user access token (flake input fetches) ────
    # PER-HOST opt-in (infra.githubAccessToken). The old model provisioned
    # the machine-user token on EVERY host whenever the fleet-wide setting
    # was on — which made it the single widest secret in the consumer fleet
    # (58 of 59 hosts) for a capability almost none of them use: fetching
    # private GitHub flake inputs during a LOCAL nix build. Hosts that pull
    # closures from the deployer or the binary cache never authenticate to
    # GitHub at all. fleet.settings.githubAccessTokens remains as the
    # fleet-wide DEFAULT for the per-host option, so existing consumers
    # keep their behaviour until they flip the default off and opt hosts in.
    sops.secrets."integrations/github/machine_user_token" =
      lib.mkIf config.infra.githubAccessToken (sopsLib.mkSecret {});
    sops.templates."nix-access-tokens" = lib.mkIf config.infra.githubAccessToken (sopsLib.mkTemplate {
      content = "access-tokens = github.com=${p."integrations/github/machine_user_token"}";
      # World-readable: required so non-root build users (e.g.
      # hydra-queue-runner on the builder host) can fetch from github
      # via flake inputs. The token is used for github API fetches only;
      # protected at the host level by SSH access controls (sssd +
      # platform-admins).
      mode = "0444";
    });

    environment.etc."nix/access-tokens.conf" = lib.mkIf config.infra.githubAccessToken {
      source = config.sops.templates."nix-access-tokens".path;
    };

    nix.extraOptions = ''
      !include /etc/nix/access-tokens.conf
    '';

    # ── nixpkgs allow-list ───────────────────────────────────────
    nixpkgs.config.allowUnfreePredicate = pkg:
      builtins.elem (lib.getName pkg) [ "consul" "timescaledb" "claude-code" ];

    # ── Telemetry agent ──────────────────────────────────────────
    # On by default only when the fleet actually declares telemetry
    # sinks — a fleet without an observability stack keeps Alloy off.
    infra.observability.alloy.enable = lib.mkDefault
      (config.fleet.settings.observability.prometheusRemoteWriteUrl != null
       && config.fleet.settings.observability.lokiPushUrl != null);

    # ── NTP via fleet chrony server ──────────────────────────────
    # Every fleet host runs chrony as a CLIENT pointing at
    # fleet.network.ntp_server. The host acting as the fleet chrony
    # SERVER overrides this with the public pool + `allow` directive
    # in its own host config.
    #
    # mkDefault lets per-host overrides win without lib.mkForce —
    # the NTP-server host sets `services.chrony.servers =
    # [ "pool.ntp.org" ... ]` at standard priority, which beats this
    # mkDefault.
    #
    # Lives in infra rather than core so bootstrap-image templates
    # don't try to reach the fleet NTP host before they're on the
    # fleet network; bootstrap install windows are short enough that
    # NTP drift doesn't matter.
    #
    # Unprivileged LXCs can't `adjtimex()` (Operation not permitted)
    # so chronyd crashloops and the deploy ends in colmena error 4
    # even when the rest of activation succeeded. The kernel clock
    # comes from the PVE host, so the LXC doesn't need NTP itself —
    # leave services.chrony off and rely on the host's chrony.
    services.chrony = {
      enable = lib.mkDefault
        (!config.boot.isContainer && config.fleet.network.ntp_server != null);
      servers = lib.mkDefault
        (lib.optional (config.fleet.network.ntp_server != null)
          config.fleet.network.ntp_server);
    };
  };
}
