{ config, lib, pkgs, fleetLib, ... }:

# NixOS LXC template factory (INFRA-86 / ADR-047).
#
# Replaces the old hand-run `sk bootstraps`/tofu-file-upload path for
# getting a NixOS LXC template onto PVE. The template is built from the
# images component family (fleetLib.images.mkBootstrapImage, target
# "proxmox-lxc") and published to the NFS template store that every PVE
# node mounts cluster-wide as the `nix-store` SR.
#
# NB: the bootstrap image is pinned to FLEETKIT's nixpkgs (nixos-generators
# `follows`), not this host's — see the note in nix/lib/tf/proxmox.nix. It
# no longer tracks the fleet's pinned NixOS version; it tracks fleetkit's,
# which is the intended "bootstrap = pinned-library role" split. The
# version label below is therefore provenance ("which fleet generation
# published this"), not the image's own nixpkgs.
#
# Two artifacts land in the vztmpl dir, named from the ADR-0003 reference
# (fleetLib.images.templates.proxmox-lxc):
#   * <name>-latest.tar.xz                 — stable "latest" alias (the
#     bpg/proxmox emitter references this name verbatim, so a container
#     apply always boots the newest published template)
#   * <name>-<nixos-version>.tar.xz        — version-labelled archive, kept
#     for rollback / provenance.
#
# "A version of NixOS available that is not on disk" is decided by a
# `.storepath` marker next to the latest alias: if it already names the
# current template's realized store path, the run is a no-op. So the
# service only does work when the deployed pin produces a template that
# hasn't been published yet — i.e. after a `nix flake update` bumps
# fleetkit + redeploy of this host, or the first run after the NFS export
# comes up.

let
  inherit (lib) mkEnableOption mkOption mkIf types;
  cfg = config.infra.build.lxcTemplateFactory;

  # The images-family bootstrap LXC template + its ADR-0003 reference.
  lxcRef = fleetLib.images.templatesData.proxmox-lxc;
  template = fleetLib.images.mkBootstrapImage {
    target = "proxmox-lxc";
    deployKey = config.fleet.network.sysadmin_ssh_key;
    inherit (config.fleet.settings.cache) substituters trustedPublicKeys;
  };
  version = config.system.nixos.version;

  latestName = lxcRef.latest;                              # <name>-latest.tar.xz
  versionedName = "${lxcRef.name}-${version}${lxcRef.ext}"; # <name>-<ver>.tar.xz

  publishScript = pkgs.writeShellApplication {
    name = "publish-lxc-template";
    runtimeInputs = [ pkgs.coreutils ];
    text = ''
      set -euo pipefail
      dir="${cfg.nfsTemplateDir}"
      # nixos-generators emits a version-suffixed name under tarball/.
      src="$(echo ${template}/tarball/*.tar.xz)"
      marker="$dir/${latestName}.storepath"

      if [ ! -d "$dir" ]; then
        echo "template dir $dir absent — NFS export not ready; skipping"
        exit 0
      fi

      want="$(readlink -f "$src")"
      if [ -f "$marker" ] && [ "$(cat "$marker")" = "$want" ]; then
        echo "up to date: $want already published (${versionedName}); nothing to do"
        exit 0
      fi

      echo "publishing NixOS LXC template ${version} from $want"
      # Atomic-ish: write to a temp name in the same dir, then rename.
      vtmp="$dir/.${versionedName}.tmp.$$"
      install -m0644 "$want" "$vtmp"
      mv -f "$vtmp" "$dir/${versionedName}"

      ltmp="$dir/.${latestName}.tmp.$$"
      install -m0644 "$want" "$ltmp"
      mv -f "$ltmp" "$dir/${latestName}"

      printf '%s\n' "$want" > "$marker"
      echo "published: ${versionedName} + ${latestName} (latest alias)"
    '';
  };
in
{
  options.infra.build.lxcTemplateFactory = {
    enable = mkEnableOption "automatic NixOS LXC template builds published to the NFS template store";

    nfsTemplateDir = mkOption {
      type = types.str;
      default = "/data/nfs/store/template/cache";
      description = "PVE vztmpl directory inside the NFS export to publish templates into.";
    };

    interval = mkOption {
      type = types.str;
      default = "daily";
      description = "systemd OnCalendar cadence for the freshness check.";
    };
  };

  config = mkIf cfg.enable {
    systemd.services.lxc-template-factory = {
      description = "Build + publish NixOS LXC template to the NFS store (INFRA-86)";
      # The NFS export must be live and /data mounted before publishing —
      # otherwise we'd write the template onto the bare root mountpoint.
      after = [ "nfs-server.service" ];
      requires = [ "nfs-server.service" ];
      unitConfig.RequiresMountsFor = [ cfg.nfsTemplateDir ];
      serviceConfig = {
        Type = "oneshot";
        ExecStart = lib.getExe publishScript;
      };
    };

    systemd.timers.lxc-template-factory = {
      description = "Periodic NixOS LXC template freshness check (INFRA-86)";
      wantedBy = [ "timers.target" ];
      timerConfig = {
        OnCalendar = cfg.interval;
        OnBootSec = "5min";
        Persistent = true;
      };
    };
  };
}
