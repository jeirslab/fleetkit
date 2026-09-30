{ config, lib, ... }:

# Remote build offload — the client half of infra.build.builder.
#
# The builder module makes a host that CAN build for others (harmonia
# serving its /nix/store, generous max-jobs, trusted-users). It does not
# make anything USE it. Substituters only help when the path is already
# built; the first host to need a path still builds it locally. On a
# 2-core/2 GB app container that is how a deploy dies: not out of disk,
# but with sshd starved out while npm links a native addon.
#
# This module is the other half. It points a host's nix-daemon at the
# fleet's build machines so heavy derivations are built THERE and the
# result copied back.
#
# PER-HOST OPT-IN, deliberately. Offloading requires an SSH private key
# on the client, and a key on every host is a fleet-wide credential for
# a capability most hosts never exercise. ../../base/fleet-member.nix
# already carries the scar from doing this the other way with the GitHub
# machine-user token ("the single widest secret in the consumer fleet,
# 58 of 59 hosts, for a capability almost none of them use"). Same shape,
# same answer: the fleet DECLARES machines in fleet.settings.build.machines;
# a host OPTS IN here.
#
# The machine's side of the trust is not managed here. The sshUser's
# authorized_keys on the builder must contain the public half of
# `sshKeySopsPath`, and that user must be in the builder's
# infra.build.builder.trustedUsers. Both are builder-side facts, so they
# live in the builder host's own config.

let
  inherit (lib) mkEnableOption mkOption mkIf types;
  cfg = config.infra.build.remote;
  sopsLib = import ../../../../lib/sops.nix { inherit lib; };

  # A host must never offload to itself: nix would SSH to its own daemon
  # and deadlock the build it is already running. Match on both names a
  # fleet machine can be known by, since fleet.settings.build.machines
  # carries an IP while the host knows itself by hostname.
  selfNames = [
    config.networking.hostName
    (config.infra.networking.internalIp or "")
    (config.infra.networking.externalIp or "")
  ];
  # Site-resolved (fleet.sites.<site>.build.machines wins over the
  # estate-wide list): a host offloads to ITS site's builder.
  machines = builtins.filter
    (m: !(builtins.elem m.hostName selfNames))
    config.fleet.self.settings.build.machines;
in
{
  options.infra.build.remote = {
    enable = mkEnableOption "offloading nix builds to the fleet's remote build machines";

    sshKeySopsPath = mkOption {
      type = types.str;
      default = "services/builder/ssh_priv_key";
      description = "Sops key path of the private key the nix-daemon uses to reach the build machines. Read as root, since the daemon — not the invoking user — opens the connection.";
    };

    sshKeySopsFile = mkOption {
      type = types.nullOr types.path;
      default = null;
      example = "../secrets/build-offload.yaml";
      description = "Encrypted file holding <sshKeySopsPath>. Null reads it from the consumer's sops.defaultSopsFile. A split-store consumer — one file per resource, each encrypted to only the hosts that need it — sets no defaultSopsFile at all, so leaving this null there fails eval with `sops.defaultSopsFile was accessed but has no value defined`. That is the point of the split: the offload key is a fleet-wide credential and belongs in a file encrypted to the few hosts that opt in, not in one every host can read.";
    };
  };

  config = mkIf cfg.enable {
    assertions = [
      {
        assertion = config.fleet.self.settings.build.machines != [ ];
        message = "infra.build.remote.enable is on but neither fleet.settings.build.machines nor this host's site declares a build machine — nothing to offload to. Declare the builder in the fleet manifest (per site under fleet.sites.<site>.build.machines), or turn this off.";
      }
    ];

    # Root-owned: nix.buildMachines' sshKey is opened by the daemon.
    sops.secrets.${cfg.sshKeySopsPath} = sopsLib.mkSecret ({
      owner = "root";
      group = "root";
      mode = "0400";
    } // lib.optionalAttrs (cfg.sshKeySopsFile != null) {
      sopsFile = cfg.sshKeySopsFile;
    });

    # Empty `machines` (this host IS the only builder) leaves
    # distributedBuilds off rather than handing nix an empty machine list.
    nix.distributedBuilds = machines != [ ];

    nix.buildMachines = map
      (m: {
        inherit (m) hostName systems maxJobs speedFactor supportedFeatures;
        protocol = "ssh-ng";
        sshUser = m.sshUser;
        sshKey = config.sops.secrets.${cfg.sshKeySopsPath}.path;
      } // lib.optionalAttrs (m.publicHostKey != null) {
        publicHostKey = m.publicHostKey;
      })
      machines;

    # Without this the CLIENT fetches every dependency and uploads it to
    # the builder — on a small container that is the same memory and disk
    # pressure we are trying to escape, just moved earlier in the build.
    # With it, the builder pulls its own inputs from the substituters.
    nix.settings.builders-use-substitutes = true;
  };
}
