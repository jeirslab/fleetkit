# services.fleetkit-reviewer: the reviewer of a deploy server's verified runs
# (`fleetkit reviewer`, cli/fleetkit_cli/reviewer.py; ADR-167 decision 5). The
# deploy server's http runner (FLEETKIT_RUNNER=http://<this host>:<port>/)
# posts review.requested here; headless Claude Code reviews the run with the
# plugin's review-run skill and this site's notes, and its verdict is posted
# with a reviewer's scoped token (runs: review), which cannot deploy.
#
# Put it on a host of its own, never on the deploy server, and give the host
# read-only access only: an SSH login without sudo on the hosts it checks
# (ssh.*), the site's logs and metrics. Everything secret comes from files or
# environmentFile at run time, never from a Nix value:
#   FLEETKIT_RUNNER_TOKEN(_FILE)    the bearer the deploy server sends
#   FLEETKIT_REVIEWER_TOKEN(_FILE)  the reviewer's API token
#   ANTHROPIC_API_KEY or CLAUDE_CODE_OAUTH_TOKEN  the model's credential
# claude-code is unfree: the host allows it (nixpkgs.config), or sets `agent`.
{ self }:
{
  config,
  lib,
  pkgs,
  ...
}:
let
  cfg = config.services.fleetkit-reviewer;
  inherit (lib) mkOption types;
  home = "/var/lib/fleetkit-reviewer";
  port = lib.last (lib.splitString ":" cfg.listen);
  notes = pkgs.writeText "fleetkit-reviewer-notes.md" cfg.notes;
  sshConfig = pkgs.writeText "fleetkit-reviewer-ssh-config" ''
    Host *
      User ${cfg.ssh.user}
      IdentityFile ${cfg.ssh.keyFile}
      IdentitiesOnly yes
      BatchMode yes
      ConnectTimeout 10
      StrictHostKeyChecking accept-new
      UserKnownHostsFile ${home}/known_hosts
      ForwardAgent no
  '';
in
{
  options.services.fleetkit-reviewer = {
    enable = lib.mkEnableOption "the reviewer of a fleetkit deploy server's verified runs";
    package = mkOption {
      type = types.package;
      default = self.packages.${pkgs.stdenv.hostPlatform.system}.fleetkit;
      defaultText = lib.literalExpression "fleetkit's package";
    };
    agent = mkOption {
      type = types.package;
      default = pkgs.claude-code;
      defaultText = lib.literalExpression "pkgs.claude-code";
      description = "Claude Code, whose `claude` reviews each run.";
    };
    model = mkOption {
      type = types.nullOr types.str;
      default = null;
      description = "The agent's --model; null: its default.";
    };
    server = mkOption {
      type = types.str;
      example = "http://10.0.0.4:8740";
      description = "The deploy server's URL.";
    };
    listen = mkOption {
      type = types.str;
      default = "0.0.0.0:8741";
      description = "host:port the runner endpoint listens on.";
    };
    openFirewall = mkOption {
      type = types.bool;
      default = false;
      description = "Open the port (restrict the source at the network: only the deploy server calls it).";
    };
    environmentFile = mkOption {
      type = types.str;
      description = "EnvironmentFile with the tokens and the model's credential (header), or the *_FILE paths to them.";
    };
    notes = mkOption {
      type = types.lines;
      default = "";
      description = ''
        What the agent is told about this site, after the skill: the hosts
        it may reach and how, where logs and metrics are, what each host
        runs. No secret: it is in the store.
      '';
    };
    ssh = {
      user = mkOption {
        type = types.str;
        default = "fleet-review";
        description = "The read-only account on the hosts (no sudo).";
      };
      keyFile = mkOption {
        type = types.str;
        description = "Path of its private key, outside the store, readable by the service.";
      };
    };
    path = mkOption {
      type = types.listOf types.package;
      default = [ ];
      description = "More tools on the agent's PATH (it has ssh, curl, jq).";
    };
  };

  config = lib.mkIf cfg.enable {
    users.users.fleetkit-reviewer = {
      isSystemUser = true;
      group = "fleetkit-reviewer";
      inherit home;
    };
    users.groups.fleetkit-reviewer = { };

    systemd.tmpfiles.rules = [
      "d ${home}/.ssh 0700 fleetkit-reviewer fleetkit-reviewer -"
      "L+ ${home}/.ssh/config - - - - ${sshConfig}"
    ];

    systemd.services.fleetkit-reviewer = {
      description = "fleetkit reviewer of verified runs";
      wantedBy = [ "multi-user.target" ];
      wants = [ "network-online.target" ];
      after = [ "network-online.target" ];
      path = [
        cfg.agent
        pkgs.openssh
        pkgs.curl
        pkgs.jq
        pkgs.coreutils
      ]
      ++ cfg.path;
      environment = {
        HOME = home;
        FLEETKIT_SERVER = cfg.server;
        FLEETKIT_REVIEWER_LISTEN = cfg.listen;
        FLEETKIT_REVIEWER_DIR = "${home}/reviews";
        FLEETKIT_REVIEWER_PROMPT_FILE = "${../plugin/skills/review-run/SKILL.md}";
        FLEETKIT_REVIEWER_NOTES_FILE = "${notes}";
        DISABLE_AUTOUPDATER = "1";
        DISABLE_TELEMETRY = "1";
      }
      // lib.optionalAttrs (cfg.model != null) { FLEETKIT_REVIEWER_MODEL = cfg.model; };
      serviceConfig = {
        ExecStart = "${cfg.package}/bin/fleetkit reviewer";
        User = "fleetkit-reviewer";
        Group = "fleetkit-reviewer";
        EnvironmentFile = cfg.environmentFile;
        StateDirectory = "fleetkit-reviewer";
        StateDirectoryMode = "0700";
        Restart = "always";
        RestartSec = 10;
        NoNewPrivileges = true;
        ProtectSystem = "strict";
        ProtectHome = true;
        PrivateTmp = true;
        PrivateDevices = true;
        ProtectKernelTunables = true;
        ProtectControlGroups = true;
        RestrictSUIDSGID = true;
      };
    };

    networking.firewall.allowedTCPPorts = lib.optional cfg.openFirewall (lib.toInt port);
  };
}
