{ config, lib, pkgs, ... }:
let
  inherit (lib) mkEnableOption mkOption mkIf mkDefault types;
  cfg = config.infra.ingress;
  sopsLib = import ../../../lib/sops.nix { inherit lib; };

  domain = config.fleet.settings.domain.internal;
  devDomain = config.fleet.settings.domain.base;

  forwardAuthBlock = lib.optionalString (cfg.forwardAuth != null && cfg.forwardAuth.outpostUrl != null) ''
    forward_auth ${cfg.forwardAuth.outpostUrl} {
      uri /outpost.goauthentik.io/auth/caddy
      copy_headers Remote-User Remote-Groups Remote-Name Remote-Email
    }
  '';

  # Build virtualHosts from services declared by other infra modules.
  # The host's own <hostname>.<domain.internal> vhost is included unconditionally
  # so every Caddy host gets an internal-CA cert for its bare hostname, even when
  # only public vhosts are declared. Path-services share that vhost via
  # `handle` blocks; otherwise it returns a minimal identity response — the
  # cert issuance is the real payload. Manual <hostname>.<domain.internal> entries
  # in virtualHosts override this default via the (serviceVhosts // cfg.virtualHosts)
  # merge below.
  serviceVhosts =
    let
      rootServices  = lib.filterAttrs (_: svc: svc.path == "/" || svc.path == "") cfg.services;
      pathServices  = lib.filterAttrs (_: svc: svc.path != "/" && svc.path != "") cfg.services;
      pathHostname  = "${config.networking.hostName}.${domain}";
      pathServiceBlocks = lib.concatStringsSep "\n" (lib.mapAttrsToList (_: svc:
        let upstream = "http://${svc.host}:${toString svc.port}";
        in ''
          handle ${svc.path}* {
            reverse_proxy ${upstream}
          }''
      ) pathServices);
      hostnameContent =
        if cfg.redirectPveToDev then
          "redir https://${config.networking.hostName}.${devDomain}{uri} 301"
        else if pathServices != {} then
          pathServiceBlocks
        else
          ''respond "${pathHostname}" 200'';
    in
    lib.mapAttrs' (_: svc: {
      name  = "${svc.name}.${domain}";
      value.extraConfig =
        if cfg.redirectPveToDev
        then "redir https://${svc.name}.${devDomain}{uri} 301"
        else "reverse_proxy http://${svc.host}:${toString svc.port}";
    }) rootServices
    // {
      "${pathHostname}".extraConfig = hostnameContent;
    };

  # ADR-026 — DNS-01 provider for public *.<domain.base> LE certs. Selected by
  # cfg.devCertIssuer (per-host). "cloudflare" is the legacy raw-token path
  # being retired; "acmedns" uses a scoped per-host acme-dns credential so
  # the zone-wide Cloudflare token lives on no host.
  hostName = config.networking.hostName;
  dnsPlugin =
    if cfg.devCertIssuer == "acmedns"
    then {
      # TODO(ADR-026): replace fakeHash with the real hash printed on the
      # first `nix build` of a host that opts into the acmedns issuer.
      module = "github.com/caddy-dns/acmedns";
      hash = lib.fakeHash;
    }
    else {
      module = "github.com/caddy-dns/cloudflare@v0.2.4";
      hash = "sha256-Olz4W84Kiyldy+JtbIicVCL7dAYl4zq+2rxEOUTObxA=";
    };
  dnsDirective =
    if cfg.devCertIssuer == "acmedns"
    then ''
      dns acmedns {
        username {env.ACME_DNS_USERNAME}
        password {env.ACME_DNS_PASSWORD}
        subdomain {env.ACME_DNS_SUBDOMAIN}
        server_url ${cfg.acmeDnsApiBase}
      }''
    else "dns cloudflare {env.CF_API_TOKEN}";
in
{
  options.infra.ingress = {
    enable = mkEnableOption "Caddy reverse proxy with automatic HTTPS";

    dataDir = mkOption {
      type = types.str;
      default = "/var/lib/caddy";
      description = "Directory for Caddy data (certs, OCSP staples).";
    };

    email = mkOption {
      type = types.nullOr types.str;
      default = config.fleet.settings.acmeEmail;
      defaultText = lib.literalExpression "config.fleet.settings.acmeEmail";
      description = "Email for ACME account registration. Must be non-null when infra.ingress is enabled (asserted).";
    };

    acmeCA = mkOption {
      type = types.nullOr types.str;
      default = config.fleet.self.settings.internalCa.acmeDirectory;
      defaultText = lib.literalExpression "config.fleet.self.settings.internalCa.acmeDirectory";
      example = "https://ca.example.internal:9000/acme/acme/directory";
      description = "ACME CA directory URL (default: the site-resolved fleet.settings.internalCa.acmeDirectory, so an ingress at a second site orders from that site's own internal CA — INFRA-307). null ⇒ Let's Encrypt production.";
    };

    acmeCARootCert = mkOption {
      type = types.nullOr types.str;
      default = if config.fleet.settings.internalCa.certFile != null
                then "${config.fleet.settings.internalCa.certFile}" else null;
      defaultText = lib.literalMD "`config.fleet.settings.internalCa.certFile` (as a string) when set, else `null`";
      description = "Path to the ACME CA's root certificate (default: fleet.settings.internalCa.certFile). Required with an internal CA.";
    };

    # Populated by other infra modules (komodo, grafana, etc.)
    services = mkOption {
      type = types.attrsOf (types.submodule {
        options = {
          name = mkOption {
            type = types.str;
            description = "Service name — becomes the FQDN prefix (<name>.<fleet.settings.domain.internal>).";
          };
          port = mkOption {
            type = types.port;
            description = "Backend port on this host.";
          };
          path = mkOption {
            type = types.str;
            default = "/";
            description = "URL path. '/' = own vhost. '/foo' = path under hostname vhost.";
          };
          host = mkOption {
            type = types.str;
            default = "127.0.0.1";
            description = "Upstream host. Defaults to localhost.";
          };
        };
      });
      default = {};
      example = lib.literalExpression ''
        { grafana = { name = "grafana"; port = 3000; }; }
      '';
      description = "Services to reverse-proxy. Set automatically by infra modules.";
    };

    virtualHosts = mkOption {
      type = types.attrsOf (types.submodule {
        options = {
          extraConfig = mkOption {
            type = types.lines;
            default = "";
            description = "Caddyfile directives for this virtual host.";
          };
        };
      });
      default = {};
      example = lib.literalExpression ''
        { "wiki.example.pve".extraConfig = "reverse_proxy http://127.0.0.1:8000"; }
      '';
      description = "Additional manual virtual host definitions.";
    };

    # Public vhosts use Let's Encrypt via Cloudflare DNS-01 instead of internal CA.
    # No public port access needed — ACME challenge is DNS-based.
    publicVirtualHosts = mkOption {
      type = types.attrsOf (types.submodule {
        options = {
          extraConfig = mkOption {
            type = types.lines;
            default = "";
            description = "Caddyfile directives for this public virtual host.";
          };
        };
      });
      default = {};
      example = lib.literalExpression ''
        { "app.example.dev".extraConfig = "reverse_proxy http://127.0.0.1:8080"; }
      '';
      description = "Virtual hosts with Let's Encrypt certs via Cloudflare DNS-01.";
    };

    devDomain = mkOption {
      type = types.bool;
      default = true;
      description = "Auto-add public base-domain vhosts with Let's Encrypt DNS-01 certs alongside the internal-domain ones.";
    };

    redirectPveToDev = mkOption {
      type = types.bool;
      default = false;
      description = "Emit 301 redirects from internal-domain service vhosts to their public base-domain equivalents.";
    };

    forwardAuth = mkOption {
      type = types.nullOr (types.submodule {
        options = {
          outpostUrl = mkOption {
            type = types.nullOr types.str;
            default = config.fleet.settings.auth.outpostUrl;
            defaultText = lib.literalExpression "config.fleet.settings.auth.outpostUrl";
            description = "Authentik embedded outpost base URL. null ⇒ no forward_auth injected.";
          };
          exclude = mkOption {
            type = types.listOf types.str;
            default = [ "auth" "headscale" ];
            description = "Service names (caddy.hostname values) to skip forward_auth. auth and headscale excluded by default to avoid redirect loops.";
          };
        };
      });
      default = {};
      description = "When set, inject Authentik forward_auth into all auto-generated public base-domain vhosts. Defaults to enabled with the fleet's configured outpost (fleet.settings.auth.outpostUrl). Set to null to disable.";
    };

    globalConfig = mkOption {
      type = types.lines;
      default = "";
      description = "Extra global Caddyfile directives.";
    };

    # ADR-026 — DNS-01 provider for public base-domain certs.
    devCertIssuer = mkOption {
      type = types.enum [ "cloudflare" "acmedns" ];
      default = "cloudflare";
      description = ''
        DNS-01 provider for public base-domain Let's Encrypt certs:
          - "cloudflare" (default, legacy): raw zone-wide Cloudflare token on
            this host. Being retired per ADR-026.
          - "acmedns": scoped per-host acme-dns credential (from
            `fleet pki acme-dns register <host>`), no Cloudflare token on the
            host. Flip per-host during rollout, then make it the default.
      '';
    };

    acmeDnsApiBase = mkOption {
      type = types.str;
      default = "http://ingress.${config.fleet.settings.domain.internal}:8081";
      defaultText = lib.literalExpression ''"http://ingress.''${config.fleet.settings.domain.internal}:8081"'';
      description = "acme-dns update API base URL (the edge acme-dns host). Used by the acmedns DNS-01 provider to write _acme-challenge TXT records.";
    };

    # INFRA-105 / ADR-059 — public LE certs over HTTP-01 / TLS-ALPN-01 for
    # names whose authoritative DNS we DON'T control (so DNS-01 is impossible),
    # e.g. a zone parked at a registrar without API access. The only requirement is that the name
    # resolves to this host and :80/:443 are reachable from the internet.
    publicHttp01VirtualHosts = mkOption {
      type = types.attrsOf (types.submodule {
        options = {
          extraConfig = mkOption {
            type = types.lines;
            default = "";
            description = "Caddyfile directives for this vhost (TLS handled by the injected Let's Encrypt HTTP-01/TLS-ALPN-01 issuer).";
          };
        };
      });
      default = {};
      example = lib.literalExpression ''
        { "www.example.org".extraConfig = "reverse_proxy http://127.0.0.1:8080"; }
      '';
      description = ''
        Public virtual hosts whose Let's Encrypt cert is obtained over
        HTTP-01 / TLS-ALPN-01 — no DNS provider, no Cloudflare token, no
        acme-dns credential, no wildcard. Use for public names whose DNS is
        not in a zone we control. Each vhost overrides the
        global acme_ca (internal step-ca) with the public LE directory.
      '';
    };

    letsencryptDirectory = mkOption {
      type = types.str;
      default = "https://acme-v02.api.letsencrypt.org/directory";
      example = "https://acme-staging-v02.api.letsencrypt.org/directory";
      description = "ACME directory for public Let's Encrypt issuance (publicHttp01VirtualHosts). Point at the staging directory to test without burning prod rate limits.";
    };
  };

  # Auto-enable Caddy only when something actually needs to be served. The old
  # condition included `cfg.devDomain` (default true), which silently enabled
  # Caddy on every host even when nothing was registered. Per-host certificate
  # acquisition for hosts without proxied services is handled by the host-cert
  # module via security.acme — no reverse proxy required.
  config = lib.mkMerge [
    {
      assertions = [
        {
          assertion = !cfg.enable || domain != null;
          message = "infra.ingress is enabled but fleet.settings.domain.internal is null — caddy names its internal vhosts <name>.<domain.internal>.";
        }
        {
          assertion = !cfg.enable || cfg.email != null;
          message = "infra.ingress is enabled but infra.ingress.email is null — set fleet.settings.acmeEmail (or infra.ingress.email) for ACME account registration.";
        }
        {
          assertion = !(cfg.devDomain && cfg.services != {}) || devDomain != null;
          message = "infra.ingress.devDomain is set with registered services but fleet.settings.domain.base is null — public base-domain vhosts need it (or set infra.ingress.devDomain = false).";
        }
      ];
    }

    (mkIf (cfg.services != {} || cfg.virtualHosts != {} || cfg.publicVirtualHosts != {} || cfg.publicHttp01VirtualHosts != {}) {
      infra.ingress.enable = mkDefault true;
    })

    # When devDomain is enabled, auto-generate public base-domain vhosts.
    # forward_auth is injected when forwardAuth is configured, skipping excluded services.
    (mkIf (cfg.devDomain && cfg.services != {}) {
      infra.ingress.publicVirtualHosts = lib.mapAttrs' (_: svc: {
        name = "${svc.name}.${devDomain}";
        value.extraConfig =
          let
            upstream  = "http://${svc.host}:${toString svc.port}";
            useAuth   = cfg.forwardAuth != null && !lib.elem svc.name cfg.forwardAuth.exclude;
            authBlock = lib.optionalString useAuth forwardAuthBlock;
          in if svc.path == "/"
             then authBlock + "reverse_proxy ${upstream}"
             else ''
               ${authBlock}handle ${svc.path}* {
                 reverse_proxy ${upstream}
               }'';
      }) (lib.filterAttrs (_: svc: svc.path == "/" || svc.path == "") cfg.services);
    })

    (mkIf cfg.enable (lib.mkMerge [
    {
    services.caddy = {
      enable = true;
      email = cfg.email;
      dataDir = cfg.dataDir;
      globalConfig = let
        # When public vhosts exist, don't set acme_ca_root globally — it
        # overrides system trust for ALL TLS including Let's Encrypt.
        hasPublic = cfg.publicVirtualHosts != {} || cfg.devDomain || cfg.publicHttp01VirtualHosts != {};
      in ''
        ${lib.optionalString (cfg.acmeCA != null) "acme_ca ${cfg.acmeCA}"}
        ${lib.optionalString (cfg.acmeCARootCert != null && !hasPublic) "acme_ca_root ${cfg.acmeCARootCert}"}
        servers {
          metrics
        }
        ${cfg.globalConfig}
      '';
      # Merge auto-generated service vhosts with manual virtualHosts.
      virtualHosts = lib.mapAttrs (_: vhost: {
        extraConfig = vhost.extraConfig;
      }) (serviceVhosts // cfg.virtualHosts);
    };

    # If step-ca is on this host, Caddy must start after it (needs the root cert).
    systemd.services.caddy = lib.mkIf (config.infra.pki.ca.enable or false) {
      after = [ "step-ca.service" ];
      wants = [ "step-ca.service" ];
    };

    systemd.tmpfiles.rules = [
      "d ${cfg.dataDir} 0750 caddy caddy -"
    ];

    networking.firewall.allowedTCPPorts = [ 80 443 ];

    # Ship Caddy metrics to Prometheus via Alloy
    infra.observability.alloy.extraConfig = ''

      // ── Caddy reverse proxy metrics ─────────────────
      // The `host` label is REQUIRED (INFRA-128): every Caddy host scrapes
      // its admin API at the same loopback 127.0.0.1:2019 as job="caddy", so
      // without a per-host label all hosts' caddy_* series collapse into one
      // and Prometheus rejects the late arrivals as "out of order sample".
      // Those 400s poison the whole remote_write batch, silently dropping
      // OTHER metrics from the same host (this is what blocked the backend-v2
      // engine/ledger scrape in INFRA-129).
      prometheus.scrape "caddy" {
        targets = [
          {"__address__" = "127.0.0.1:2019", "host" = constants.hostname},
        ]
        forward_to = [prometheus.remote_write.default.receiver]
        scrape_interval = "15s"
        metrics_path = "/metrics"
        job_name = "caddy"
      }
    '';
    }

    # ── Public virtual hosts (Let's Encrypt via DNS-01) ──
    # The DNS-01 provider is selected by cfg.devCertIssuer (ADR-026): the
    # legacy "cloudflare" raw-token path, or the "acmedns" scoped-credential
    # path that keeps the Cloudflare token off the host.
    (mkIf (cfg.publicVirtualHosts != {}) (lib.mkMerge [
      {
        # Caddy built with the DNS plugin matching the chosen issuer.
        services.caddy.package = pkgs.caddy.withPlugins {
          plugins = [ dnsPlugin.module ];
          hash = dnsPlugin.hash;
        };

        # Wrap each public vhost with the LE issuer + DNS-01 provider.
        # Overrides the global acme_ca (step-ca) with Let's Encrypt.
        services.caddy.virtualHosts = lib.mapAttrs (_: vhost: {
          extraConfig = ''
            tls {
              issuer acme {
                dir https://acme-v02.api.letsencrypt.org/directory
                ${dnsDirective}
              }
            }
            ${vhost.extraConfig}
          '';
        }) cfg.publicVirtualHosts;
      }

      # Legacy: raw zone-wide Cloudflare token (being retired per ADR-026).
      (mkIf (cfg.devCertIssuer == "cloudflare") {
        sops.secrets."integrations/cloudflare/api_token" = sopsLib.mkSecret {
          restartUnits = [ "caddy.service" ];
        } // { owner = "caddy"; };

        systemd.services.caddy.serviceConfig.EnvironmentFile =
          config.sops.templates."caddy-cloudflare-env".path;

        sops.templates."caddy-cloudflare-env" = sopsLib.mkTemplate {
          owner = "caddy";
          content = "CF_API_TOKEN=${config.sops.placeholder."integrations/cloudflare/api_token"}";
          restartUnits = [ "caddy.service" ];
        };
      })

      # ADR-026: scoped per-host acme-dns credential — no Cloudflare token.
      # Secrets are minted by `fleet pki acme-dns register <host>`; until then
      # this host cannot deploy with devCertIssuer = "acmedns" (sops-nix
      # refuses missing secrets at activation — register first).
      (mkIf (cfg.devCertIssuer == "acmedns") (
        let credPath = field: "integrations/acme-dns/${hostName}/${field}"; in {
          sops.secrets.${credPath "username"}  = sopsLib.mkSecret { restartUnits = [ "caddy.service" ]; } // { owner = "caddy"; };
          sops.secrets.${credPath "password"}  = sopsLib.mkSecret { restartUnits = [ "caddy.service" ]; } // { owner = "caddy"; };
          sops.secrets.${credPath "subdomain"} = sopsLib.mkSecret { restartUnits = [ "caddy.service" ]; } // { owner = "caddy"; };

          systemd.services.caddy.serviceConfig.EnvironmentFile =
            config.sops.templates."caddy-acmedns-env".path;

          sops.templates."caddy-acmedns-env" = sopsLib.mkTemplate {
            owner = "caddy";
            content = ''
              ACME_DNS_USERNAME=${config.sops.placeholder.${credPath "username"}}
              ACME_DNS_PASSWORD=${config.sops.placeholder.${credPath "password"}}
              ACME_DNS_SUBDOMAIN=${config.sops.placeholder.${credPath "subdomain"}}
            '';
            restartUnits = [ "caddy.service" ];
          };
        }
      ))
    ]))

    # ── Public HTTP-01 / TLS-ALPN-01 vhosts (Let's Encrypt, no DNS provider) ──
    # For public names whose authoritative DNS we do NOT control (so DNS-01 is
    # impossible). Each vhost overrides the
    # global acme_ca (internal step-ca) with the public LE directory and lets
    # Caddy solve the challenge over :80/:443 — no DNS plugin, no token, no
    # wildcard. The name must resolve to this host and :80/:443 must be
    # internet-reachable. (INFRA-105 / ADR-059)
    (mkIf (cfg.publicHttp01VirtualHosts != {}) {
      services.caddy.virtualHosts = lib.mapAttrs (_: vhost: {
        extraConfig = ''
          tls {
            issuer acme {
              dir ${cfg.letsencryptDirectory}
            }
          }
          ${vhost.extraConfig}
        '';
      }) cfg.publicHttp01VirtualHosts;
    })
    ]))
  ];
}
