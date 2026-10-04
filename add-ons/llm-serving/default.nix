# infra.addons.llmServing — LLM-serving building blocks. Today: the model cold
# store, a Hugging Face pull-through cache (Olah) that downloads each model from
# the internet once, onto an always-on host, and serves every other host from
# the LAN. It does NOT download faster than the link it crosses. (Tested: with
# the upstream unreachable Olah 0.5.1 answers 504 even for a cached file, so
# do not count on it as an offline archive; survival of an upstream removal
# has not been tested.)
#
# Inert unless `infra.addons.llmServing.coldStore.enable`. Nothing is fetched
# from a flake input: Olah is built from pinned PyPI sources by ./overlays.nix,
# which this module adds to the host's `nixpkgs.overlays` itself when enabled.
# That (rather than asking the consumer to) is deliberate: mkFleet's
# colmenaOverlays reach only the colmena meta nixpkgs, never the
# nixosConfigurations, and a module-level overlay is the one place that works
# for both.
#
# Clients set HF_ENDPOINT=http://<mirrorNetloc> and HF_HUB_DISABLE_XET=1 (see
# the read-only `clientEnv`). Tested limits are documented on that option.
#
# Options live in ./options.nix.
{ config, lib, pkgs, ... }:
let
  inherit (lib) mkIf;
  cfg = config.infra.addons.llmServing.coldStore;

  repos = "${cfg.dataDir}/repos";
  logs = "${cfg.dataDir}/logs";
in
{
  config = lib.mkMerge [
    # Always defined (a read-only option takes exactly one definition).
    {
      infra.addons.llmServing.coldStore.clientEnv =
        if cfg.enable && cfg.mirrorNetloc != null then {
          HF_ENDPOINT = "http://${cfg.mirrorNetloc}";
          HF_HUB_DISABLE_XET = "1";
        } else { };
    }

    (mkIf cfg.enable {
      assertions = [
        {
          assertion = cfg.mirrorNetloc != null;
          message = "infra.addons.llmServing.coldStore.enable is set, but mirrorNetloc is not: set it to the host:port clients use to reach the cache (Olah rewrites redirects with it).";
        }
      ];

      nixpkgs.overlays = [ (import ./overlays.nix) ];

      # A static user, not DynamicUser: a dynamic user's state directory lives
      # under /var/lib/private, which broke bind-mounted and ZFS-backed data
      # dirs in practice.
      users.users.olah = {
        isSystemUser = true;
        group = "olah";
        home = cfg.dataDir;
      };
      users.groups.olah = { };

      systemd.tmpfiles.rules = [
        "d ${cfg.dataDir} 0755 olah olah - -"
        "d ${repos} 0755 olah olah - -"
        "d ${logs} 0755 olah olah - -"
      ];

      systemd.services.olah = {
        description = "Olah Hugging Face pull-through cache (model cold store)";
        wantedBy = [ "multi-user.target" ];
        after = [ "network-online.target" ];
        wants = [ "network-online.target" ];
        serviceConfig = {
          User = "olah";
          Group = "olah";
          WorkingDirectory = cfg.dataDir;
          # No --cache-size-limit unless asked: a COLD store must never evict.
          # mirror-netloc is the address clients use; Olah rewrites redirects
          # (including LFS ones) with it.
          ExecStart = lib.escapeShellArgs ([
            "${pkgs.olah}/bin/olah-cli"
            "--host" cfg.listenAddress
            "--port" (toString cfg.port)
            "--mirror-netloc" cfg.mirrorNetloc
            "--mirror-lfs-netloc" cfg.mirrorNetloc
            "--repos-path" repos
            "--log-path" logs
          ] ++ lib.optionals (cfg.cacheSizeLimit != null) [
            "--cache-size-limit" cfg.cacheSizeLimit
          ]);
          Restart = "on-failure";
          RestartSec = 5;
        };
      };

      networking.firewall.allowedTCPPorts = mkIf cfg.openFirewall [ cfg.port ];

      environment.systemPackages = mkIf cfg.prefetch.enable [
        (import ./prefetch.nix { inherit pkgs; inherit (cfg) port; })
      ];
    })
  ];
}
