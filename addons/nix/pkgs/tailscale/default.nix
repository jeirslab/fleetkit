# fleetkit.addons.tailscale: the Tailscale client from a package the host
# chooses, the node's TLS certificate (`tailscale cert`), and the Headscale
# server settings that make `tailscale cert` work on a Headscale tailnet.
# Off by default; the rest of the host's tailscale config stays its own
# (services.tailscale.*).
{
  config,
  lib,
  pkgs,
  ...
}:
let
  inherit (lib) mkOption mkEnableOption types mkIf;
  cfg = config.fleetkit.addons.tailscale;
  cert = cfg.cert;
  hs = cfg.headscale.certificates;
  tailscaleBin = lib.getExe' config.services.tailscale.package "tailscale";
in
{
  options.fleetkit.addons.tailscale = {
    enable = mkEnableOption "the Tailscale client (services.tailscale.enable, with `package`)";

    package = mkOption {
      type = types.package;
      default = pkgs.tailscale;
      defaultText = lib.literalExpression "pkgs.tailscale";
      description = ''
        The Tailscale package, set as services.tailscale.package. Point it at a newer build
        than nixpkgs' (for example from a separate nixpkgs flake) to run that one.
      '';
    };

    cert = {
      enable = mkEnableOption "the node's TLS certificate from `tailscale cert`, renewed daily";

      domain = mkOption {
        type = types.str;
        default = "";
        example = "dumpy.tamarin-gray.ts.net";
        description = "The node's MagicDNS name. Required when cert.enable is set.";
      };

      dir = mkOption {
        type = types.path;
        default = "/var/lib/tailscale-cert";
        description = "Where the certificate and key are written.";
      };

      group = mkOption {
        type = types.str;
        default = "tailscale-cert";
        description = ''
          The group that may read the key (mode 0640, root:group). Created if the host does not
          declare it; add the services that need the key to it.
        '';
      };

      reloadUnits = mkOption {
        type = types.listOf types.str;
        default = [ ];
        example = [ "caddy.service" ];
        description = "Units reloaded (restarted if they cannot reload) when a renewal changed the files.";
      };

      certFile = mkOption {
        type = types.path;
        readOnly = true;
        default = "${cert.dir}/${cert.domain}.crt";
        defaultText = lib.literalExpression ''"''${cert.dir}/''${cert.domain}.crt"'';
        description = "The certificate's final path.";
      };

      keyFile = mkOption {
        type = types.path;
        readOnly = true;
        default = "${cert.dir}/${cert.domain}.key";
        defaultText = lib.literalExpression ''"''${cert.dir}/''${cert.domain}.key"'';
        description = "The key's final path.";
      };
    };

    headscale.certificates = {
      enable = mkEnableOption ''
        the DNS-01 settings a Headscale server needs to hand out node certificates, rendered
        into services.headscale.settings (merged). Needs a Headscale that has the feature
        (juanfont/headscale#3554)
      '';

      zone = mkOption {
        type = types.str;
        example = "example.com";
        description = "The public DNS zone the provider manages (dns.certificates.zone). dns.base_domain must be a public domain in it.";
      };

      challenge.provider = {
        name = mkOption {
          type = types.enum [
            "cloudflare"
            "gigahost"
            "rfc2136"
            "webhook"
          ];
          example = "cloudflare";
          description = "The DNS provider that writes the ACME DNS-01 records (dns.challenge.provider.name).";
        };
        apiTokenFile = mkOption {
          type = types.path;
          example = "/run/credentials/headscale.service/cloudflare-token";
          description = ''
            The file holding the provider's API token, rendered as api_token_path. A path, never
            the value: load it into the unit, for example with
            systemd.services.headscale.serviceConfig.LoadCredential.
          '';
        };
      };

      minVersion = mkOption {
        type = types.str;
        default = "0.34.0";
        description = "The first Headscale version with dns.certificates; an older services.headscale.package gets a warning.";
      };
    };
  };

  config = lib.mkMerge [
    (mkIf cfg.enable {
      services.tailscale.enable = true;
      services.tailscale.package = cfg.package;
    })

    (mkIf (cfg.enable && cert.enable) {
      assertions = [
        {
          assertion = cert.domain != "";
          message = "fleetkit.addons.tailscale.cert.domain must be set (the node's MagicDNS name).";
        }
      ];

      users.groups.${cert.group} = { };

      systemd.services.tailscale-cert = {
        description = "Tailscale node certificate for ${cert.domain}";
        after = [
          "tailscaled.service"
          "network-online.target"
        ];
        wants = [
          "tailscaled.service"
          "network-online.target"
        ];
        wantedBy = [ "multi-user.target" ];
        path = [
          pkgs.coreutils
          pkgs.systemd
        ];
        serviceConfig = {
          Type = "oneshot";
          Restart = "on-failure";
          RestartSec = "60s";
          UMask = "0027";
        };
        startLimitIntervalSec = 3600;
        startLimitBurst = 10;
        script = ''
          install -d -m 0750 -o root -g ${lib.escapeShellArg cert.group} ${lib.escapeShellArg cert.dir}
          before=$(cat ${lib.escapeShellArg cert.certFile} 2>/dev/null | sha256sum || true)
          ${tailscaleBin} cert \
            --cert-file ${lib.escapeShellArg cert.certFile} \
            --key-file ${lib.escapeShellArg cert.keyFile} \
            ${lib.escapeShellArg cert.domain}
          chown root:${lib.escapeShellArg cert.group} ${lib.escapeShellArg cert.certFile} ${lib.escapeShellArg cert.keyFile}
          chmod 0644 ${lib.escapeShellArg cert.certFile}
          chmod 0640 ${lib.escapeShellArg cert.keyFile}
          after=$(sha256sum < ${lib.escapeShellArg cert.certFile})
          if [ "$before" != "$after" ]; then
            ${lib.concatMapStringsSep "\n" (u: "systemctl try-reload-or-restart ${lib.escapeShellArg u}") cert.reloadUnits}
            true
          fi
        '';
      };

      systemd.timers.tailscale-cert = {
        description = "Renew the Tailscale node certificate daily";
        wantedBy = [ "timers.target" ];
        timerConfig = {
          OnCalendar = "daily";
          Persistent = true;
          RandomizedDelaySec = "1h";
        };
      };
    })

    (mkIf hs.enable {
      services.headscale.settings.dns = {
        certificates = {
          enabled = true;
          inherit (hs) zone;
        };
        challenge.provider = {
          inherit (hs.challenge.provider) name;
          api_token_path = toString hs.challenge.provider.apiTokenFile;
        };
      };

      warnings = lib.optional (
        lib.versionOlder (lib.getVersion config.services.headscale.package) hs.minVersion
      ) "fleetkit.addons.tailscale.headscale.certificates: services.headscale.package is ${lib.getVersion config.services.headscale.package}, older than ${hs.minVersion}, the first version with dns.certificates; the settings will be ignored or refused.";
    })
  ];
}
