{ config, lib, ... }:
let
  inherit (lib) mkOption types;
  cfg = config.infra.pki.hostCert;
  hostName = config.networking.hostName;
  caddyOwns = config.services.caddy.enable or false;
  internalDomain = config.fleet.settings.domain.internal;
in
{
  options.infra.pki.hostCert.enable = mkOption {
    type = types.bool;
    default = true;
    example = false;
    description = ''
      Acquire a per-host internal cert for this machine. Has no effect unless
      the fleet declares an internal CA (fleet.settings.internalCa.acmeDirectory)
      and this host is not a Caddy host — those two conditions still gate it.

      Set false on a host with no network path to the internal CA. The ACME
      order then does not run at all, rather than retrying on a timer and
      leaving a permanently failed unit behind a self-signed fallback nothing
      is consuming. The case this exists for is a host in a second site whose
      own CA is not up yet: the CA address in fleet.settings is fleet-wide, so
      such a host chases a name it cannot resolve.
    '';
  };

  # Per-host internal cert for <hostname>.<domain.internal>, acquired via
  # the internal CA's (step-ca) ACME directory. Stored at
  # /var/lib/acme/<host>/ so services that want TLS (postgres SSL, future
  # mTLS clients) can read the cert+key off disk without needing a
  # reverse proxy in front.
  #
  # Skipped on Caddy hosts: Caddy already binds port 80 for its own ACME
  # challenges and issues this cert as part of its declared vhost set.
  #
  # Also skipped entirely when the fleet declares no internal CA
  # (fleet.settings.internalCa.acmeDirectory = null): a minimum-viable
  # fleet without step-ca must not have every host chase Let's Encrypt
  # for an internal-only name.
  config = lib.mkIf
    (cfg.enable && !caddyOwns && config.fleet.settings.internalCa.acmeDirectory != null) {
    assertions = [
      {
        assertion = config.fleet.settings.acmeEmail != null;
        message = "host-cert: fleet.settings.internalCa.acmeDirectory is set but fleet.settings.acmeEmail is null — ACME registration against the internal CA needs an account email.";
      }
      {
        assertion = internalDomain != null;
        message = "host-cert: fleet.settings.internalCa.acmeDirectory is set but fleet.settings.domain.internal is null — per-host certs are issued for <hostname>.<domain.internal>.";
      }
    ];

    security.acme = {
      acceptTerms = true;
      defaults.email = config.fleet.settings.acmeEmail;
      defaults.server = config.fleet.settings.internalCa.acmeDirectory;
      certs."${hostName}.${internalDomain}" = {
        domain = "${hostName}.${internalDomain}";
        listenHTTP = ":80";
      };
    };

    networking.firewall.allowedTCPPorts = [ 80 ];
  };
}
