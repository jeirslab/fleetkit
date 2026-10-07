# services.fleetkit: the deploy server (fleetkit serve) on a NixOS host, in
# GitOps mode: it mirrors the estate repo and deploys the branch, on a GitHub
# push webhook or by polling. Experimental (docs/pulumi.md).
#
# The host holds what a deploy needs, so treat it like the operator's machine:
# the age key that decrypts the estate's secrets, the SSH key Colmena deploys
# with, a read-only deploy key for the repo, the Pulumi passphrase, and the API
# token. All of them come from files (environmentFile, or paths in it); none is
# a Nix value, so none reaches the store.
{ self }:
{
  config,
  lib,
  pkgs,
  ...
}:
let
  cfg = config.services.fleetkit;
  inherit (lib) mkOption types;
in
{
  options.services.fleetkit = {
    enable = lib.mkEnableOption "the fleetkit deploy server";
    package = mkOption {
      type = types.package;
      default = self.packages.${pkgs.stdenv.hostPlatform.system}.fleetkit;
      defaultText = lib.literalExpression "fleetkit.packages.\${system}.fleetkit";
      description = "The fleetkit package.";
    };
    repo = mkOption {
      type = types.str;
      example = "git@github.com:example/estate.git";
      description = "The estate repo's git URL.";
    };
    branch = mkOption {
      type = types.str;
      default = "main";
      description = "The branch that is the desired state.";
    };
    deployOnPush = mkOption {
      type = types.listOf types.str;
      default = [ ];
      example = [ "homelab" ];
      description = "Estates deployed (or previewed, see pushMode) when the branch moves.";
    };
    pushMode = mkOption {
      type = types.enum [
        "deploy"
        "preview"
      ];
      default = "preview";
      description = "deploy: apply every push. preview: plan every push; apply with POST /v1/deploys.";
    };
    poll = mkOption {
      type = types.ints.unsigned;
      default = 300;
      description = "Seconds between fetches of the branch; 0 relies on the webhook alone.";
    };
    listen = mkOption {
      type = types.str;
      default = "127.0.0.1:8740";
      description = "host:port of the API (put a TLS proxy in front for anything but loopback).";
    };
    workers = mkOption {
      type = types.ints.positive;
      default = 2;
      description = "Estates deploying at once.";
    };
    environmentFile = mkOption {
      type = types.path;
      example = "/run/secrets/fleetkit.env";
      description = ''
        Environment file (systemd EnvironmentFile) with the secrets, e.g.
        FLEETKIT_API_TOKEN_FILE, PULUMI_CONFIG_PASSPHRASE_FILE,
        FLEETKIT_WEBHOOK_SECRET_FILE, SOPS_AGE_KEY_FILE, and GIT_SSH_COMMAND /
        NIX_SSHOPTS naming the deploy keys.
      '';
    };
    stateDir = mkOption {
      type = types.str;
      default = "/var/lib/fleetkit";
      description = "Mirror, checkouts, work dirs, local Pulumi state and job logs.";
    };
  };

  config = lib.mkIf cfg.enable {
    users.users.fleetkit = {
      isSystemUser = true;
      group = "fleetkit";
      home = cfg.stateDir;
    };
    users.groups.fleetkit = { };
    # Evaluating and building the estate's systems goes through the daemon.
    nix.settings.trusted-users = [ "fleetkit" ];

    systemd.services.fleetkit = {
      description = "fleetkit deploy server";
      wantedBy = [ "multi-user.target" ];
      after = [ "network-online.target" ];
      wants = [ "network-online.target" ];
      path = [
        config.nix.package
        pkgs.git
        pkgs.openssh
      ];
      environment = {
        FLEETKIT_REPO = cfg.repo;
        FLEETKIT_BRANCH = cfg.branch;
        FLEETKIT_DEPLOY_ON_PUSH = lib.concatStringsSep "," cfg.deployOnPush;
        FLEETKIT_PUSH_MODE = cfg.pushMode;
        FLEETKIT_POLL = toString cfg.poll;
        FLEETKIT_STATE_DIR = cfg.stateDir;
        PULUMI_HOME = "${cfg.stateDir}/pulumi-home";
        HOME = cfg.stateDir;
      };
      serviceConfig = {
        ExecStart = "${lib.getExe cfg.package} serve --listen ${cfg.listen} --workers ${toString cfg.workers}";
        EnvironmentFile = cfg.environmentFile;
        User = "fleetkit";
        Group = "fleetkit";
        StateDirectory = lib.mkIf (cfg.stateDir == "/var/lib/fleetkit") "fleetkit";
        Restart = "on-failure";
        NoNewPrivileges = true;
        ProtectSystem = "strict";
        ProtectHome = true;
        ReadWritePaths = [ cfg.stateDir ];
        PrivateTmp = true;
      };
    };
  };
}
