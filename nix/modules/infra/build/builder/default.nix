{ config, lib, pkgs, ... }:
let
  inherit (lib) mkEnableOption mkOption mkIf types;
  cfg = config.infra.build.builder;
  sopsLib = import ../../../../lib/sops.nix { inherit lib; };
in
{
  options.infra.build.builder = {
    enable = mkEnableOption "Nix remote builder + harmonia binary cache";

    # 4 jobs × 2 cores fills the 8-vCPU container without single-threading
    # large derivations (bitcoin-core, rust workspaces). The old default
    # of 8 jobs × 1 core meant any one build was stuck on a single core.
    maxJobs = mkOption {
      type = types.int;
      default = 4;
      description = "Maximum number of parallel nix build jobs.";
    };

    cores = mkOption {
      type = types.int;
      default = 2;
      description = "Number of CPU cores per build job.";
    };

    trustedUsers = mkOption {
      type = types.listOf types.str;
      default = [ "root" "sysadmin" ];
      description = "Users trusted to manage the Nix store.";
    };

    cacheBindAddress = mkOption {
      type = types.str;
      default = "[::]:5000";
      description = "Address:port harmonia binary cache listens on.";
    };
    offload = {
      user = mkOption {
        type = types.str;
        default = "nix-offload";
        description = "Unprivileged account that offloading hosts (infra.build.remote) connect as; added to trusted-users so the daemon accepts their store operations. Created only when authorizedKeys is non-empty. Name it in the machine entry's `sshUser`.";
      };
      authorizedKeys = mkOption {
        type = types.listOf types.str;
        default = [];
        example = [ "ssh-ed25519 AAAA… fleet-offload" ];
        description = "Public halves of the offload keys (the private half is the consumers' services/builder/ssh_priv_key). Empty = no offload account; remote builds then need root's own key, which is the operator key and does not belong on hosts.";
      };
    };
  };

  config = mkIf cfg.enable {
    # Docker, for builders that also produce container images. mkDefault,
    # not a hard set: a builder that only ever produces nix derivations
    # (jeirslab's does) should be able to say so and drop the daemon plus
    # its closure, without mkForce-ing its way out of the framework.
    infra.integrations.docker.enable = lib.mkDefault true;

    users.users = lib.mkIf (cfg.offload.authorizedKeys != []) {
      ${cfg.offload.user} = {
        isNormalUser = true;
        description = "nix remote-build offload account";
        openssh.authorizedKeys.keys = cfg.offload.authorizedKeys;
      };
    };
    nix.settings = {
      max-jobs = cfg.maxJobs;
      cores = cfg.cores;
      trusted-users = cfg.trustedUsers
        ++ lib.optional (cfg.offload.authorizedKeys != []) cfg.offload.user;
      substituters = lib.mkAfter [
        "https://nix-community.cachix.org"
      ];
      trusted-public-keys = lib.mkAfter [
        "nix-community.cachix.org-1:mB9FSh9qf2dCimDSUo8Zy7bkq5CX+/rkCWyvRCYg3Fs="
      ];
    };

    # Cache signing key persisted in SOPS so a rebuild of the build CT
    # restores the SAME key — pubkey stays stable across redeploys, no
    # fleet-wide trusted-public-keys update required. Previously the
    # nix-cache-keygen systemd-service generated a fresh key whenever
    # /var/lib/nix-cache/ was empty, which forced a cumbersome rotation
    # on every from-scratch CT rebuild.
    #
    # Rotation now: regenerate the key pair locally, replace the SOPS
    # entry, replace nix/secrets/secrets/keys/builder-cache-pub-key.pem,
    # deploy the build host, then fleet-redeploy.
    # Deliberately root-owned. Since nixpkgs 26.11 harmonia runs as a
    # `DynamicUser`, so there is no static `harmonia` user at activation
    # time — naming one as `owner` makes sops-install-secrets fail
    # *manifest validation*, which takes down every secret on the host,
    # not just this one. The unit reaches the key through systemd
    # `LoadCredential`, which reads it as root before dropping
    # privileges, so root ownership is both correct and required.
    sops.secrets."services/builder/cache_signing_priv_key" = sopsLib.mkSecret {
      mode = "0400";
      restartUnits = [ "harmonia.service" ];
    };

    # nixpkgs 26.05 moved harmonia under the `cache` sub-attr. Both
    # `services.harmonia.enable` and `services.harmonia.settings` are
    # deprecated aliases of `services.harmonia.cache.enable` /
    # `services.harmonia.cache.settings`. signKeyPaths was already
    # under `cache`.
    services.harmonia.cache = {
      enable = true;
      signKeyPaths = [
        config.sops.secrets."services/builder/cache_signing_priv_key".path
      ];
      settings.bind = cfg.cacheBindAddress;
    };

    # Ship Harmonia cache metrics to Prometheus
    infra.observability.alloy.extraConfig = ''

      // ── Harmonia binary cache metrics ───────────────
      prometheus.scrape "harmonia" {
        targets = [
          {"__address__" = "127.0.0.1:5000"},
        ]
        forward_to = [prometheus.remote_write.default.receiver]
        scrape_interval = "30s"
        metrics_path = "/metrics"
        job_name = "harmonia"
      }
    '';

    infra.services.cache = {
      port = 5000;
      caddy.enable = false;
      description = "Harmonia Nix binary cache";
      category = "build";
      tags = [ "nix" "internal" ];
    };
  };
}
