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
      default = "stable";
      description = ''
        The deploy branch: the only branch whose commits deploy (previews may be
        of any commit). It stands in for branch protection, which GitHub's free
        plan lacks on private repos.
      '';
    };
    previewBranches = mkOption {
      type = types.listOf types.str;
      default = [ "unstable" ];
      description = "Pull requests into these branches are previewed too (webhook path).";
    };
    deployOnPush = mkOption {
      type = types.listOf types.str;
      default = [ ];
      example = [ "homelab" ];
      description = ''
        Estates the server deploys (or previews, see pushMode) on its own when
        the branch moves (webhook or poll). Empty when GitHub Actions drives it
        (actions/deploy): the workflow names the estates.
      '';
    };
    pushMode = mkOption {
      type = types.enum [
        "deploy"
        "preview"
      ];
      default = "preview";
      description = "deploy: apply every push. preview: plan every push; apply with POST /v1/deploys.";
    };
    trigger = mkOption {
      type = types.enum [
        "push"
        "pr"
      ];
      default = "pr";
      description = ''
        What deploys. pr: a merged pull request (its merge commit), reported
        on the PR; pushes and polling deploy nothing. push: a push to the
        branch (by the webhook or polling). Either way, pull requests from
        trusted authors are previewed and reported on the PR.
      '';
    };
    publicUrl = mkOption {
      type = types.nullOr types.str;
      default = null;
      example = "https://deploy.example.com";
      description = "Where the API is reached from outside; GitHub statuses link to the job there.";
    };
    poll = mkOption {
      type = types.ints.unsigned;
      default = 0;
      description = "Seconds between fetches of the branch; 0 relies on the webhook alone.";
    };
    listen = mkOption {
      type = types.str;
      default = "127.0.0.1:8740";
      description = "host:port of the API (put a TLS proxy in front for anything but loopback).";
    };
    firewallInterface = mkOption {
      type = types.nullOr types.str;
      default = null;
      example = "tailscale0";
      description = ''
        Open the API port on this interface only (with listen on 0.0.0.0 or the
        interface's address): a server reached over the tailnet, by people and by
        CI runners that join it (actions/deploy, tailscale-authkey).
      '';
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
        FLEETKIT_WEBHOOK_SECRET_FILE, FLEETKIT_GITHUB_TOKEN_FILE (statuses and
        PR comments), SOPS_AGE_KEY_FILE, and GIT_SSH_COMMAND /
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

    networking.firewall.interfaces = lib.mkIf (cfg.firewallInterface != null) {
      ${cfg.firewallInterface}.allowedTCPPorts = [ (lib.toInt (lib.last (lib.splitString ":" cfg.listen))) ];
    };

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
        FLEETKIT_TRIGGER = cfg.trigger;
        FLEETKIT_PREVIEW_BRANCHES = lib.concatStringsSep "," cfg.previewBranches;
        FLEETKIT_STATE_DIR = cfg.stateDir;
        PULUMI_HOME = "${cfg.stateDir}/pulumi-home";
      }
      // lib.optionalAttrs (cfg.publicUrl != null) {
        FLEETKIT_PUBLIC_URL = cfg.publicUrl;
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
