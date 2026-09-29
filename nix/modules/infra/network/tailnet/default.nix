{ config, lib, pkgs, ... }:
let
  inherit (lib) mkEnableOption mkOption mkIf mkMerge types optional concatMap;
  cfg = config.infra.network.tailnet;
  sopsLib = import ../../../../lib/sops.nix { inherit lib; };
  headscalePolicy = import ../../../../lib/headscale-policy.nix { inherit lib; };
  # The fleet-manifest entry for this host (null if absent) — used by
  # infra.network.tailnet.fleetNode to derive this node's ACL tags.
  # Indexed by the fleet KEY (fleet.self.key, injected by mkHosts), with the
  # hostname as a fallback for evaluations outside mkHosts (the plain
  # component checks). A renamed or CTID-keyed host has hostname != key.
  fleetEntry =
    let key = config.fleet.self.key or null;
    in if key != null && (config.fleet.compute ? ${key}) then config.fleet.compute.${key}
       else config.fleet.compute.${config.networking.hostName} or null;
  p = config.sops.placeholder;

  # Initial-enrollment flags: handed to the upstream
  # tailscaled-autoconnect.service which calls `tailscale up --auth-key=...`.
  # Tags are EXCLUDED here because headscale v2 rejects the registration
  # if tag ownership doesn't resolve at register time (a known quirk for
  # users without an OIDC subject — see hosts.nix netgate notes). Tags
  # are applied later by tailscale-postup.service against the now-existing
  # node identity, where the validation path is different.
  #
  # `--reset` is included so that an autoconnect run on a node with stale
  # prefs (e.g. a previously-registered node that got logged out) does
  # not fail with "changing settings via 'tailscale up' requires
  # mentioning all non-default flags". Without --reset the upstream
  # autoconnect refuses to re-enroll a node carrying any non-default
  # pref (advertised tags, hostname overrides) — observed 2026-05-21 on
  # jupyterhub. With --reset the node always converges to the declared
  # enrollment state; tags then re-apply via tailscale-postup or the
  # 5-min headscale-tag-reconciler.
  enrollFlags =
    [ "--reset" ]
    ++ optional cfg.advertiseExitNode "--advertise-exit-node"
    ++ optional (cfg.advertiseRoutes != []) "--advertise-routes=${lib.concatStringsSep "," cfg.advertiseRoutes}"
    ++ cfg.extraUpFlags;

  # Post-enrollment flag set: enrollFlags plus any --advertise-tags. This
  # is the canonical "what the node should look like" set that postup
  # reconciles toward.
  upFlags = enrollFlags
    ++ optional (cfg.advertiseTags != []) "--advertise-tags=${lib.concatStringsSep "," cfg.advertiseTags}";

  # Hash of the desired up-flag set — used as a systemd restartTrigger
  # so a flag change forces tailscale-postup.service to re-run, and as
  # the "current state" stamp written to disk after a successful up.
  upFlagsString = lib.concatStringsSep " " upFlags;
  upFlagsHash = builtins.hashString "sha256" upFlagsString;
in
{
  options.infra.network.tailnet = {
    enable = mkEnableOption "Tailscale client";

    fleetNode = mkOption {
      type = types.bool;
      default = false;
      description = ''
        One-line fleet tailnet enrollment. When true this implies
        `enable` and derives the following — all via `mkDefault`, so
        any individual knob stays overridable per-host:
          * `userspace = true`  — LXC containers have no /dev/net/tun
          * `fetchAuthKey.url`  — fleet.settings.tailnet.preauthKeyUrl
                                  (when set; else SOPS auth key)
          * `advertiseTags`     — this host's ACL tags, from the fleet
                                  manifest via lib/headscale-policy.nix
          * `extraUpFlags`      — --login-server (from
                                  fleet.settings.tailnet.controlUrl),
                                  --hostname=<host>, --accept-dns=false
        Turns the per-host tailnet rollout into a single line per
        host regardless of that host's module structure.
      '';
    };

    authKeyFile = mkOption {
      type = types.nullOr types.path;
      default = null;
      example = lib.literalExpression ''config.sops.secrets."services/tailscale/preauth_key".path'';
      description = "Path to file containing Tailscale/Headscale preauth key.";
    };

    tailnetName = mkOption {
      type = types.nullOr types.str;
      default = null;
      example = "fleet-bot";
      description = "Tailnet or Headscale namespace name.";
    };

    advertiseExitNode = mkOption {
      type = types.bool;
      default = false;
      description = "Advertise this node as an exit node.";
    };

    advertiseRoutes = mkOption {
      type = types.listOf types.str;
      default = [];
      example = [ "192.0.2.0/24" ];
      description = "CIDR routes this node should advertise.";
    };

    advertiseTags = mkOption {
      type = types.listOf types.str;
      default = [];
      example = [ "tag:env-dev" "tag:role-postgres" ];
      description = ''
        Tailscale ACL tags this node should advertise. Each entry must
        be of the form "tag:<slug>" and the corresponding tagOwners
        rule on the headscale server must permit the host's enrollment
        user (typically "netgate@") to apply it. Tags can only be set
        via `tailscale up` — the drift-fix oneshot below re-runs `up`
        when this list changes so rebuilds reliably re-tag the node.
      '';
    };

    outboundProxy = mkOption {
      type = types.nullOr types.port;
      default = null;
      example = 1055;
      description = ''
        Local port for tailscaled's outbound HTTP proxy
        (`--outbound-http-proxy-listen`). Set this when a service on a
        USERSPACE node needs to make outbound connections to tailnet peers.

        In userspace mode tailscaled installs no kernel routes, so the host
        cannot originate ordinary TCP to a tailnet IP at all — there is no
        interface to send it out of. The node is reachable INBOUND (that is
        what `serveUI` relies on) while every outbound connection fails, which
        is a confusing asymmetry to debug: `tailscale ping` succeeds, because
        it goes through the daemon rather than the kernel datapath, while
        `curl http://<tailnet-ip>/` has nowhere to send the packet.

        With this set, point the client at http://localhost:<port> and
        outbound tailnet traffic works with no TUN device, no container
        privilege changes, and no firewall edits.
      '';
    };

    userspace = mkOption {
      type = types.bool;
      default = false;
      description = ''
        Run tailscaled in userspace networking mode
        (`--tun=userspace-networking`). Required for LXC containers
        that don't have /dev/net/tun passed through. Strongly preferred
        on LXC anyway: the Proxmox host runs its own kernel-mode
        tailscaled on a separate (cloud) tailnet, and granting LXCs
        TUN passthrough breaks that isolation.

        In userspace mode tailscaled does not install kernel routes
        for tailnet IPs, so applications must use tailscale's built-in
        proxies — `tailscale ssh`, `tailscale serve`, or the SOCKS5
        proxy at 127.0.0.1:1055 — rather than direct routing.
      '';
    };

    # ── Auto-fetched preauth key (alternative to authKeyFile via SOPS) ──
    # When set, a pre-autoconnect oneshot curls `url` and writes the
    # body to `outputPath`, then authKeyFile defaults to that path.
    # Used by fleet hosts to fetch the preauth key from netgate's
    # internal headscale endpoint instead of carrying a SOPS-encrypted
    # copy on every host. The endpoint is source-IP-restricted to the
    # internal LAN; trust model = "anyone who can reach netgate over
    # port 443 can join the tailnet" (same as our existing SOPS posture).
    fetchAuthKey = mkOption {
      type = types.nullOr (types.submodule {
        options = {
          url = mkOption {
            type = types.str;
            example = "https://headscale.example.lan/internal/preauth/fleet-bot";
            description = "HTTPS endpoint that returns the preauth key as raw text body.";
          };
          outputPath = mkOption {
            type = types.str;
            default = "/var/lib/tailscale-fetch/preauth.key";
            description = "Where the fetched key is persisted on disk (mode 0400).";
          };
        };
      });
      default = null;
      description = "Fetch the tailnet preauth key over HTTPS at boot instead of carrying a SOPS copy on every host: a pre-autoconnect oneshot curls `url`, writes the body to `outputPath` (mode 0400), and `authKeyFile` defaults to that path. null disables fetching.";
    };

    extraUpFlags = mkOption {
      type = types.listOf types.str;
      default = [];
      example = [ "--login-server=https://vpn.example.dev" "--accept-dns=false" ];
      description = "Extra flags appended to tailscale up.";
    };

    funnel = {
      enable = mkEnableOption "Tailscale Funnel (public internet exposure)";

      routes = mkOption {
        type = types.attrsOf (types.submodule {
          options = {
            path = mkOption {
              type = types.str;
              default = "/";
              description = "URL path to serve on the ts.net hostname.";
            };
            backendPort = mkOption {
              type = types.port;
              description = "Local port to proxy to.";
            };
            backendAddress = mkOption {
              type = types.str;
              default = "127.0.0.1";
              description = "Address the route proxies to (combined with `backendPort`). Default targets loopback on this host.";
            };
          };
        });
        default = {};
        description = "Path-based routes to expose via Tailscale Funnel.";
        example = {
          headscale = { path = "/"; backendPort = 8080; };
          headplane = { path = "/admin"; backendPort = 3100; };
        };
      };
    };

    serve = mkOption {
      type = types.attrsOf (types.submodule {
        options = {
          enable = mkOption {
            type = types.bool;
            default = true;
            description = "Enable this Tailscale Serve forwarder.";
          };
          servePort = mkOption {
            type = types.port;
            default = 443;
            description = ''
              Tailnet-side TCP port exposed on this node's MagicDNS name
              (`<hostname>.<base-domain>`). This is a raw passthrough: TLS is
              terminated by the backend (typically a local Caddy holding the
              tailnet-suffix wildcard cert), NOT by tailscaled. headscale has
              no `tailscale cert`/serve-HTTPS support (upstream #2527), so
              `--https`/`--tls-terminated-tcp` are unusable here — this is
              always a raw `--tcp` forwarder. See ADR-036.
            '';
          };
          backendPort = mkOption {
            type = types.port;
            description = "Local TCP port to forward raw bytes to.";
          };
          backendAddress = mkOption {
            type = types.str;
            default = "127.0.0.1";
            description = "Local address to forward to.";
          };
        };
      });
      default = {};
      example = lib.literalExpression ''{ caddy = { servePort = 443; backendPort = 443; }; }'';
      description = ''
        Declarative Tailscale Serve raw-TCP forwarders — tailnet-only, never
        funnelled to the public internet. Each entry exposes
        `tcp://<hostname>.<base-domain>:<servePort>` over the tailnet and
        forwards raw bytes to a local backend. In userspace mode (all fleet
        LXCs) `serve` is the only way to accept inbound tailnet connections.
        See ADR-036 (tailnet-only admin plane).
      '';
    };

    # ── serveUI: one-line tailnet-only browser UI (ADR-036) ──
    # The ergonomic front-end to `serve`. One entry wires BOTH halves of
    # the admin-plane pattern:
    #   1. a local Caddy vhost `<hostname>.<serveUIDomain>` (browser-trusted
    #      per-host LE cert via the host's devCertIssuer) reverse-proxying
    #      the backend, and
    #   2. a single raw `serve --tcp=443 → 127.0.0.1:443` forwarder so the
    #      tailnet reaches that Caddy.
    # MagicDNS only answers the node's own name, so every UI on a host
    # shares `<hostname>.<serveUIDomain>`; multiple UIs on one host are
    # disambiguated by `path` (Caddy handle blocks). At most one entry may
    # use path "/".
    serveUI = mkOption {
      type = types.attrsOf (types.submodule {
        options = {
          enable = mkOption {
            type = types.bool;
            default = true;
            description = "Expose this UI over the tailnet.";
          };
          description = mkOption {
            type = types.str;
            default = "";
            description = ''
              One-line, human-facing summary of what this UI is for.
              Surfaced in the developer handbook by
              `nix run .#wiki-services`. Keep it dev-facing (what you'd
              use it for), not infra detail. Falls back to the matching
              `infra.services.<name>.description` when left blank.
            '';
          };
          backendPort = mkOption {
            type = types.port;
            description = "Local backend port the UI listens on.";
          };
          backendAddress = mkOption {
            type = types.str;
            default = "127.0.0.1";
            description = "Local backend address.";
          };
          servePort = mkOption {
            type = types.port;
            default = 443;
            description = ''
              Tailnet-side TCP port. Reached at
              `https://<hostname>.<serveUIDomain>[:servePort]`. For a host
              running more than one UI, give each a distinct port (e.g. 443
              and 8443) so every UI sits at its own vhost root — avoids the
              sub-path breakage common to SPAs. Entries sharing a port are
              combined into one vhost and disambiguated by `path`.
            '';
          };
          path = mkOption {
            type = types.str;
            default = "/";
            description = ''
              URL path within the `servePort` vhost. "/" = the whole vhost
              (at most one per (host, servePort)). "/foo" = a `handle /foo*`
              block. Prefer distinct `servePort`s over paths unless the app
              explicitly supports a base path.
            '';
          };
        };
      });
      default = {};
      example = lib.literalExpression ''{ pgweb.backendPort = 8081; }'';
      description = ''
        Tailnet-only browser UIs. Each entry produces a Caddy vhost on this
        host's MagicDNS name with a browser-trusted per-host cert, fronted
        by `tailscale serve`. Reachable only from tailnet members (no public
        DNS record). The network is the gate — see ADR-036.
      '';
    };

    serveUIDomain = mkOption {
      type = types.nullOr types.str;
      default = config.fleet.settings.domain.tailnetSuffix;
      defaultText = lib.literalExpression "config.fleet.settings.domain.tailnetSuffix";
      description = "MagicDNS base domain for serveUI hostnames (`<hostname>.<this>`). Must be non-null when serveUI entries exist (asserted).";
    };
  };

  config = mkMerge [
    # ── serveUI → Caddy vhost + serve forwarder (ADR-036) ──
    # Translates the ergonomic serveUI entries into (1) a per-host
    # publicVirtualHost on the MagicDNS name (Caddy wraps it with the LE
    # DNS-01 issuer → browser-trusted per-host cert) and (2) one raw-TCP
    # serve forwarder feeding that Caddy. Top-level (not under cfg.enable)
    # because it writes options consumed by the caddy module; the serve
    # forwarder it emits carries the tailscale.enable assertion.
    (mkIf (cfg.serveUI != {}) (
      let
        enabled   = lib.filterAttrs (_: u: u.enable) cfg.serveUI;
        fqdn      = "${config.networking.hostName}.${cfg.serveUIDomain}";
        proxyLine = u: "reverse_proxy http://${u.backendAddress}:${toString u.backendPort}";
        # Group entries by tailnet-side port: one Caddy vhost + one serve
        # forwarder per distinct servePort.
        ports     = lib.unique (lib.mapAttrsToList (_: u: u.servePort) enabled);
        groupOn   = port: lib.filterAttrs (_: u: u.servePort == port) enabled;
        # Caddy site address: bare fqdn for 443, fqdn:port otherwise.
        vhostKey  = port: if port == 443 then fqdn else "${fqdn}:${toString port}";
        vhostBody = grp:
          let
            roots = lib.filterAttrs (_: u: u.path == "/" || u.path == "") grp;
            paths = lib.filterAttrs (_: u: u.path != "/" && u.path != "") grp;
          in lib.concatStringsSep "\n" (
            (lib.mapAttrsToList (_: u: proxyLine u) roots)
            ++ (lib.mapAttrsToList (_: u: ''
              handle ${u.path}* {
                ${proxyLine u}
              }'') paths)
          );
      in {
        infra.ingress.publicVirtualHosts = lib.listToAttrs (map (port:
          lib.nameValuePair (vhostKey port) { extraConfig = vhostBody (groupOn port); }
        ) ports);

        # One raw-TCP serve forwarder per port: tailnet :port → local Caddy :port.
        infra.network.tailnet.serve = lib.listToAttrs (map (port:
          lib.nameValuePair "_serveUI-${toString port}" {
            servePort = port;
            backendPort = port;
            backendAddress = "127.0.0.1";
          }
        ) ports);

        assertions = [{
          assertion = cfg.serveUIDomain != null;
          message = "infra.network.tailnet.serveUI entries exist on '${config.networking.hostName}' but infra.network.tailnet.serveUIDomain is null — set fleet.settings.domain.tailnetSuffix (or serveUIDomain explicitly).";
        }] ++ map (port: {
          assertion = (builtins.length (builtins.attrNames
            (lib.filterAttrs (_: u: u.path == "/" || u.path == "") (groupOn port)))) <= 1;
          message = "infra.network.tailnet.serveUI on '${config.networking.hostName}': servePort ${toString port} has more than one entry at path \"/\" — give them distinct paths or ports.";
        }) ports;
      }
    ))

    # ── fleetNode: one-line fleet tailnet enrollment ──
    # Deliberately NOT gated on cfg.enable — this is the branch that
    # *sets* enable. Every derived value uses mkDefault so any single
    # knob stays overridable per-host.
    (mkIf cfg.fleetNode (
      let tailnet = config.fleet.settings.tailnet;
      in {
        assertions = [{
          assertion = fleetEntry != null;
          message = "infra.network.tailnet.fleetNode is set on '${config.networking.hostName}' but that host is absent from config.fleet.compute.";
        }];
        infra.network.tailnet = {
          enable = lib.mkDefault true;
          userspace = lib.mkDefault true;
          advertiseTags = lib.mkDefault
            (if fleetEntry != null then headscalePolicy.tagsFor fleetEntry else []);
          # Auto-fetch the enrollment key from the fleet's preauth
          # endpoint when one is declared; otherwise the client branch
          # falls back to the SOPS-held auth key.
          fetchAuthKey = lib.mkDefault
            (if tailnet.preauthKeyUrl != null
             then { url = tailnet.preauthKeyUrl; }
             else null);
          extraUpFlags = lib.mkDefault (
            lib.optional (tailnet.controlUrl != null)
              "--login-server=${tailnet.controlUrl}"
            ++ [
              "--hostname=${config.networking.hostName}"
              "--accept-dns=false"
            ]);
        };
      }
    ))

    # ── Client ──
    (mkIf cfg.enable (mkMerge [
    {
      environment.systemPackages = [ pkgs.tailscale ];

      services.tailscale = {
        enable = true;
        authKeyFile = lib.mkIf (cfg.authKeyFile != null) cfg.authKeyFile;
        extraUpFlags = enrollFlags;  # tags applied post-enrollment by postup
        extraDaemonFlags =
          (lib.optional cfg.userspace "--tun=userspace-networking")
          ++ (lib.optional (cfg.outboundProxy != null)
               "--outbound-http-proxy-listen=localhost:${toString cfg.outboundProxy}");
        port = lib.mkDefault 41641;
        useRoutingFeatures = lib.mkDefault "client";
      };

      networking.firewall.allowedUDPPorts = [ config.services.tailscale.port ];

      sops.secrets."integrations/tailscale/auth_key" = lib.mkIf (cfg.fetchAuthKey == null) (sopsLib.mkSecret {});
      sops.templates."tailscale-auth-key" = lib.mkIf (cfg.fetchAuthKey == null) (sopsLib.mkTemplate {
        mode = "0400";
        content = p."integrations/tailscale/auth_key";
      });

      infra.network.tailnet.authKeyFile =
        if cfg.fetchAuthKey != null
        then lib.mkDefault cfg.fetchAuthKey.outputPath
        else lib.mkDefault config.sops.templates."tailscale-auth-key".path;

      # ── Auto-fetch preauth key from internal endpoint ──
      # Runs before tailscaled-autoconnect so the file exists when
      # autoconnect reads it. Re-runs on every activation; the endpoint
      # serves a stable reusable key so re-fetching is a no-op string
      # write. Network-online ordering matters because we need DNS for
      # the headscale hostname to resolve via the fleet's CoreDNS — a
      # fresh container has no /etc/hosts entry for it.
      systemd.services.tailscale-fetch-key = lib.mkIf (cfg.fetchAuthKey != null) {
        description = "Fetch tailscale preauth key from internal headscale endpoint";
        before = [ "tailscaled-autoconnect.service" ];
        after = [ "network-online.target" ];
        wants = [ "network-online.target" ];
        requiredBy = [ "tailscaled-autoconnect.service" ];
        path = [ pkgs.curl pkgs.coreutils ];
        # Validate the body looks like a real headscale preauth key.
        # Headscale serves "hskey-auth-..." for a hit; Caddy serves a
        # blank/HTML page (HTTP 200) when source-IP allowlist matcher
        # rejects OR when the vhost is missing entirely. `curl -f`
        # alone can't tell those apart from a real key — both are 200.
        # Without this check the unit succeeds with a 0-byte/HTML file,
        # tailscale up runs with --auth-key="" → falls back to interactive
        # AuthURL → autoconnect times out → operator gets a confusing
        # "Logged out. Log in at: https://..." error message rather than
        # the actionable "preauth endpoint returned non-preauth body".
        script = ''
          set -euo pipefail
          out="${cfg.fetchAuthKey.outputPath}"
          mkdir -p "$(dirname "$out")"
          # Retry briefly — DNS / CoreDNS might race with us on first boot.
          for i in $(seq 1 5); do
            if curl -fsS --max-time 15 -o "$out.tmp" "${cfg.fetchAuthKey.url}"; then
              body=$(cat "$out.tmp")
              case "$body" in
                hskey-auth-*)
                  chmod 0400 "$out.tmp"
                  mv "$out.tmp" "$out"
                  echo "tailscale-fetch-key: wrote $out ($(wc -c < "$out") bytes)"
                  exit 0
                  ;;
                *)
                  echo "tailscale-fetch-key: endpoint returned $(wc -c < "$out.tmp") bytes that don't look like a preauth key (first 60: $(head -c 60 "$out.tmp")); retrying"
                  rm -f "$out.tmp"
                  ;;
              esac
            else
              echo "tailscale-fetch-key: fetch attempt $i failed, retrying"
            fi
            sleep 5
          done
          echo "tailscale-fetch-key: gave up after 5 attempts"
          exit 1
        '';
        serviceConfig = {
          Type = "oneshot";
          RemainAfterExit = true;
        };
        # Force re-run on every Colmena deploy: the script body change
        # alone normally triggers it (NixOS restarts units whose unit
        # files change), but tying restartTriggers to the URL guarantees
        # a re-run if anyone widens the validation or changes endpoint.
        # Combined with the prefix check above, a broken on-disk key
        # (from an earlier deploy that captured a Caddy stub body) gets
        # replaced on the next deploy without manual SSH remediation.
        restartTriggers = [ cfg.fetchAuthKey.url ];
        # After a successful fetch, kick tailscaled-autoconnect even if
        # it's in failed state from a previous deploy where the on-disk
        # key was a stub. MUST use --no-block: tailscaled-autoconnect
        # has `Requires=tailscale-fetch-key.service`, so a blocking
        # `systemctl restart` from inside our postStart deadlocks
        # systemd (it refuses to restart a dependent unit mid-transaction
        # for the unit it depends on). With --no-block we enqueue the
        # restart and return immediately; systemd processes it after
        # this transaction completes. Autoconnect short-circuits on
        # State=Running so re-firing on every fetch is cheap.
        postStart = ''
          ${pkgs.systemd}/bin/systemctl reset-failed tailscaled-autoconnect.service 2>/dev/null || true
          ${pkgs.systemd}/bin/systemctl --no-block restart tailscaled-autoconnect.service 2>/dev/null || true
        '';
      };

      # ── Up-flag drift reconciliation ──
      # The upstream tailscaled-autoconnect.service short-circuits with
      # "Tailscale is running" once the node is authenticated, so changes
      # to extraUpFlags / advertiseTags / advertiseRoutes after first
      # enrollment never re-apply. This oneshot rectifies that:
      #   * restartTriggers includes the up-flag hash, so a flag change
      #     forces systemd to restart the unit on activation.
      #   * The script compares desired hash vs the value persisted in
      #     /var/lib/tailscale-postup/upflags.hash. If different, it
      #     re-runs `tailscale up` with the full declared flag set
      #     plus --reset (so unset settings revert to defaults), then
      #     stamps the new hash.
      # Net effect: `tailscale debug prefs` always reflects what the
      # Nix config declares, including --advertise-tags which would
      # otherwise require a manual operator intervention every rebuild.
      systemd.services.tailscale-postup = {
        description = "Reconcile Tailscale up-flag drift on rebuild";
        after = [ "tailscaled-autoconnect.service" "tailscaled.service" ];
        requires = [ "tailscaled.service" ];
        wantedBy = [ "multi-user.target" ];
        path = [ pkgs.tailscale pkgs.jq pkgs.coreutils ];
        restartTriggers = [ upFlagsHash ];
        script = ''
          set -euo pipefail

          stateDir=/var/lib/tailscale-postup
          stateFile="$stateDir/upflags.hash"
          mkdir -p "$stateDir"

          # Wait for tailscaled to be authenticated and online before
          # attempting to reconcile prefs. Initial enrollment is owned
          # by tailscaled-autoconnect; we run after it.
          for i in $(seq 1 30); do
            if tailscale status --json 2>/dev/null | jq -e '.Self.Online' >/dev/null 2>&1; then
              break
            fi
            sleep 2
          done

          desired="${upFlagsHash}"
          current=""
          [ -f "$stateFile" ] && current=$(cat "$stateFile")

          if [ "$desired" = "$current" ]; then
            echo "tailscale-postup: up-flag hash unchanged ($desired) — no action"
            exit 0
          fi

          echo "tailscale-postup: drift detected (desired=$desired current=''${current:-<none>})"

          auth_key_arg=()
          ${lib.optionalString (cfg.authKeyFile != null) ''
            if [ -r "${cfg.authKeyFile}" ]; then
              auth_key_arg=(--auth-key="$(cat ${cfg.authKeyFile})")
            fi
          ''}

          # No trailing --reset: enrollFlags already contains --reset
          # (which flows into upFlagsString), and `tailscale up` rejects
          # the flag appearing twice ("invalid boolean flag reset: flag
          # provided multiple times" — observed 2026-05-26 on
          # headscale-router after the --reset-in-enrollFlags change).
          tailscale up ${upFlagsString} "''${auth_key_arg[@]}"

          # Stamp success only AFTER `tailscale up` returns 0 — a failed
          # up will leave the old hash in place so the next activation
          # retries instead of silently masking the drift.
          echo "$desired" > "$stateFile"
          echo "tailscale-postup: reconciled to $desired"
        '';
        serviceConfig = {
          Type = "oneshot";
          RemainAfterExit = true;
          # Bounded timeout so a control-plane outage can never wedge boot.
          # `tailscale up --reset` blocks until the control plane confirms;
          # without this cap the unit ran with "no limit" and on netgate
          # (where the control plane *is* the same host's headscale) it
          # deadlocked multi-user.target for 5+ minutes during a fresh boot
          # before being killed externally. 90s is well above a healthy
          # enrollment round-trip but well below "operator intervention".
          TimeoutStartSec = "90s";
        };
      };
    }

    # ── Funnel (public internet exposure) ──
    (mkIf cfg.funnel.enable {
      services.tailscale.useRoutingFeatures = lib.mkForce "both";

      systemd.services.tailscale-funnel = {
        description = "Configure Tailscale Funnel routes";
        after = [ "tailscaled.service" ];
        requires = [ "tailscaled.service" ];
        wantedBy = [ "multi-user.target" ];
        path = [ pkgs.tailscale pkgs.jq ];
        script = ''
          # Wait for tailscale to authenticate and come online
          for i in $(seq 1 60); do
            if tailscale status --json 2>/dev/null | jq -e '.Self.Online' > /dev/null 2>&1; then
              break
            fi
            sleep 2
          done

          # Reset any stale serve config
          tailscale serve reset || true

          # Configure path-based routes
          ${lib.concatStringsSep "\n" (lib.mapAttrsToList (_: route:
            "tailscale serve --set-path ${route.path} http://${route.backendAddress}:${toString route.backendPort}"
          ) cfg.funnel.routes)}

          # Enable funnel (expose to public internet)
          tailscale funnel on
        '';
        serviceConfig = {
          Type = "oneshot";
          RemainAfterExit = true;
          ExecStop = "${pkgs.tailscale}/bin/tailscale funnel off";
        };
      };
    })

    # ── Serve (raw TCP passthrough, tailnet-only) ──
    # headscale has no `tailscale cert`/serve-HTTPS (upstream #2527), so we
    # never terminate TLS at tailscaled. Each entry is a raw `--tcp`
    # forwarder; TLS is terminated by the local backend (typically a Caddy
    # holding the tailnet-suffix wildcard). Modeled on tailscale-funnel
    # above but WITHOUT `funnel on` — exposure stays tailnet-internal.
    #
    # The previous implementation wrote `services.tailscale.serve` with a
    # {host,protocol,listenPort,backend} shape that is type-incompatible
    # with the nixpkgs pin's `{enable,configFile,services}` option (it was
    # dead code — used by zero hosts and would have failed to evaluate on
    # first use). Replaced with this imperative oneshot, which is the same
    # pattern the funnel path already uses successfully.
    (mkIf (cfg.serve != {}) (
      let
        enabledServe = lib.filterAttrs (_: s: s.enable) cfg.serve;
        serveCmds = lib.concatStringsSep "\n" (lib.mapAttrsToList (_: s:
          "tailscale serve --bg --tcp=${toString s.servePort} tcp://${s.backendAddress}:${toString s.backendPort}"
        ) enabledServe);
        # Re-run the unit whenever the forwarder set changes.
        serveHash = builtins.hashString "sha256" serveCmds;
      in {
        systemd.services.tailscale-serve = {
          description = "Configure Tailscale Serve raw-TCP forwarders (tailnet-only)";
          after = [ "tailscaled.service" "tailscaled-autoconnect.service" ];
          requires = [ "tailscaled.service" ];
          wantedBy = [ "multi-user.target" ];
          path = [ pkgs.tailscale pkgs.jq ];
          restartTriggers = [ serveHash ];
          script = ''
            # Wait for tailscaled to be authenticated and online before
            # touching serve config (mirrors the funnel unit).
            for i in $(seq 1 60); do
              if tailscale status --json 2>/dev/null | jq -e '.Self.Online' >/dev/null 2>&1; then
                break
              fi
              sleep 2
            done

            # Reset stale serve config so removed/renamed entries disappear,
            # then (re)apply the declared raw-TCP forwarders.
            tailscale serve reset || true
            ${serveCmds}
          '';
          serviceConfig = {
            Type = "oneshot";
            RemainAfterExit = true;
            ExecStop = "${pkgs.tailscale}/bin/tailscale serve reset";
          };
        };

        assertions = [{
          assertion = config.services.tailscale.enable;
          message = "infra.network.tailnet.serve requires infra.network.tailnet.enable = true.";
        }];
      }
    ))
    ]))
  ];
}
