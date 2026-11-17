# services.fleetkit-reviewer: the reviewer of a deploy server's verified runs
# (`fleetkit reviewer`, cli/fleetkit_cli/reviewer.py; ADR-167 decision 5). The
# deploy server's http runner (FLEETKIT_RUNNER=http://<this host>:<port>/)
# posts review.requested here; headless Claude Code reviews the run with the
# plugin's review-run skill and this site's notes, and the verdict is posted
# with a reviewer's scoped token (runs: review), which cannot deploy.
#
# Two units, two users, one spool directory between them:
#   fleetkit-reviewer        the service: the runner and reviewer tokens
#                            (environmentFile), the HTTP endpoint, the posts
#   fleetkit-review-agent    the agent: the model's credential only
#                            (agentEnvironmentFile), the SSH key, Claude Code
# so nothing the agent reads or runs can reach a token. Its Bash runs two
# commands only: `fleet-ssh <host> <command...>` (no ssh options; ssh.hosts
# only) and `fleet-get <url> [--data-urlencode name=value]...` (GET, the
# prefixes of get.allow only). Read-only on the hosts is the hosts' part:
# give the account (ssh.user) no sudo and no group that reads secrets, and
# its key `restrict` (no forwarding, no pty) and a forced command
# (`command="..."`) that runs only read-only diagnostics: fleet-ssh hands
# the command to the host as it is.
#
# Put it on a host of its own, never on the deploy server. claude-code is
# unfree: the host allows it (nixpkgs.config), or sets `agent`. A new host's
# SSH key is trusted on first use (known_hosts in the agent's home).
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
  spool = "/var/lib/fleetkit-review";
  agentHome = "/var/lib/fleetkit-review-agent";
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
      UserKnownHostsFile ${agentHome}/known_hosts
      ClearAllForwardings yes
      ForwardAgent no
      ForwardX11 no
      PermitLocalCommand no
      ProxyCommand none
      RequestTTY no
      ControlMaster no
  '';
  fleetSsh = pkgs.writeShellScriptBin "fleet-ssh" ''
    host=''${1:?usage: fleet-ssh <host> <command...>}; shift
    case "$host" in -*|*@*|*" "*) echo "fleet-ssh: not a host: $host" >&2; exit 2 ;; esac
    case " ${toString cfg.ssh.hosts} " in *" $host "*) ;; *) echo "fleet-ssh: $host is not one of: ${toString cfg.ssh.hosts}" >&2; exit 2 ;; esac
    [ $# -gt 0 ] || { echo "fleet-ssh: a command is required" >&2; exit 2; }
    exec ${pkgs.openssh}/bin/ssh -F ${sshConfig} -- "$host" "$@"
  '';
  fleetGet = pkgs.writeShellScriptBin "fleet-get" ''
    url=''${1:?usage: fleet-get <url> [--data-urlencode name=value]...}; shift
    ok=
    for p in ${lib.escapeShellArgs cfg.get.allow}; do case "$url" in "$p"*) ok=1 ;; esac; done
    [ -n "$ok" ] || { echo "fleet-get: $url is not under: ${toString cfg.get.allow}" >&2; exit 2; }
    args=()
    while [ $# -gt 0 ]; do
      # name=value only: curl reads a file for "@file" and "name@file".
      [ "$1" = --data-urlencode ] && [ $# -ge 2 ] && [[ "$2" =~ ^[A-Za-z_][A-Za-z0-9_]*= ]] \
        || { echo "fleet-get: only --data-urlencode name=value pairs" >&2; exit 2; }
      args+=(--data-urlencode "$2"); shift 2
    done
    exec ${pkgs.curl}/bin/curl -q -sS --max-time 30 --proto =http,https --max-redirs 0 -G "''${args[@]}" -- "$url"
  '';
  hardening = {
    NoNewPrivileges = true;
    ProtectSystem = "strict";
    ProtectHome = true;
    PrivateTmp = true;
    PrivateDevices = true;
    ProtectProc = "invisible";
    ProtectKernelTunables = true;
    ProtectKernelModules = true;
    ProtectKernelLogs = true;
    ProtectControlGroups = true;
    ProtectClock = true;
    ProtectHostname = true;
    RestrictNamespaces = true;
    RestrictSUIDSGID = true;
    LockPersonality = true;
    CapabilityBoundingSet = "";
    RestrictAddressFamilies = [
      "AF_INET"
      "AF_INET6"
      "AF_UNIX"
    ];
    UMask = "0007";
    Restart = "always";
    RestartSec = 10;
  };
  common = {
    FLEETKIT_REVIEWER_SPOOL = spool;
    FLEETKIT_REVIEWER_TIMEOUT = toString cfg.timeout;
  };
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
    timeout = mkOption {
      type = types.int;
      default = 1200;
      description = "Seconds an agent may take; keep it under the server's FLEETKIT_REVIEW_TIMEOUT.";
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
      description = "Open the port (better: admit the deploy server's address only).";
    };
    environmentFile = mkOption {
      type = types.str;
      description = "The service's EnvironmentFile: FLEETKIT_RUNNER_TOKEN(_FILE) and FLEETKIT_REVIEWER_TOKEN(_FILE).";
    };
    agentEnvironmentFile = mkOption {
      type = types.str;
      description = "The agent's EnvironmentFile: ANTHROPIC_API_KEY or CLAUDE_CODE_OAUTH_TOKEN, and nothing else.";
    };
    notes = mkOption {
      type = types.lines;
      default = "";
      description = ''
        What the agent is told about this site, after the skill: the hosts
        it may reach, where logs and metrics are, what each host runs. No
        secret: it is in the store.
      '';
    };
    ssh = {
      user = mkOption {
        type = types.str;
        default = "fleet-review";
        description = "The read-only account on the hosts.";
      };
      keyFile = mkOption {
        type = types.str;
        description = "Path of its private key, outside the store, readable by fleetkit-review-agent only.";
      };
      hosts = mkOption {
        type = types.listOf types.str;
        default = [ ];
        description = "The hosts fleet-ssh may reach (addresses or names); at least one.";
      };
    };
    get.allow = mkOption {
      type = types.listOf types.str;
      default = [ ];
      example = [ "http://10.0.0.17:3100/" ];
      description = "URL prefixes fleet-get may read (logs and metrics query APIs).";
    };
  };

  config = lib.mkIf cfg.enable {
    assertions = [
      {
        assertion = cfg.ssh.hosts != [ ];
        message = "services.fleetkit-reviewer.ssh.hosts: name the hosts fleet-ssh may reach";
      }
      {
        assertion = lib.all (p: builtins.match "https?://[^/@]+/.*" p != null) cfg.get.allow;
        message = "services.fleetkit-reviewer.get.allow: each prefix is scheme://host[:port]/ (a path after the host)";
      }
    ];
    users.users.fleetkit-reviewer = {
      isSystemUser = true;
      group = "fleetkit-reviewer";
      extraGroups = [ "fleetkit-review-agent" ];
    };
    users.groups.fleetkit-reviewer = { };
    users.users.fleetkit-review-agent = {
      isSystemUser = true;
      group = "fleetkit-review-agent";
      home = agentHome;
    };
    users.groups.fleetkit-review-agent = { };

    # The spool: the service writes, the agent reads its run and answers in it.
    systemd.tmpfiles.rules = [
      "d ${spool} 2750 fleetkit-reviewer fleetkit-review-agent -"
      "d ${spool}/runs 2750 fleetkit-reviewer fleetkit-review-agent 14d"
      "d ${spool}/queue 2770 fleetkit-reviewer fleetkit-review-agent -"
    ];

    systemd.services.fleetkit-reviewer = {
      description = "fleetkit reviewer of verified runs (service)";
      wantedBy = [ "multi-user.target" ];
      wants = [ "network-online.target" ];
      after = [ "network-online.target" ];
      environment = common // {
        FLEETKIT_SERVER = cfg.server;
        FLEETKIT_REVIEWER_LISTEN = cfg.listen;
      };
      serviceConfig = hardening // {
        ExecStart = "${cfg.package}/bin/fleetkit reviewer";
        User = "fleetkit-reviewer";
        Group = "fleetkit-reviewer";
        SupplementaryGroups = [ "fleetkit-review-agent" ];
        EnvironmentFile = cfg.environmentFile;
        ReadWritePaths = [ spool ];
      };
    };

    systemd.services.fleetkit-review-agent = {
      description = "fleetkit reviewer of verified runs (agent)";
      wantedBy = [ "multi-user.target" ];
      wants = [ "network-online.target" ];
      after = [ "network-online.target" ];
      path = [
        cfg.agent
        fleetSsh
        fleetGet
        pkgs.bash
        pkgs.coreutils
      ];
      environment = common // {
        HOME = agentHome;
        FLEETKIT_REVIEWER_PROMPT_FILE = "${../plugin/skills/review-run/SKILL.md}";
        FLEETKIT_REVIEWER_NOTES_FILE = "${notes}";
        FLEETKIT_REVIEWER_COMMANDS = "fleet-ssh fleet-get";
        DISABLE_AUTOUPDATER = "1";
        DISABLE_TELEMETRY = "1";
      }
      // lib.optionalAttrs (cfg.model != null) { FLEETKIT_REVIEWER_MODEL = cfg.model; };
      serviceConfig = hardening // {
        ExecStart = "${cfg.package}/bin/fleetkit reviewer --agent";
        User = "fleetkit-review-agent";
        Group = "fleetkit-review-agent";
        EnvironmentFile = cfg.agentEnvironmentFile;
        StateDirectory = "fleetkit-review-agent";
        StateDirectoryMode = "0700";
        ReadWritePaths = [ spool ];
      };
    };

    networking.firewall.allowedTCPPorts = lib.optional cfg.openFirewall (lib.toInt port);
  };
}
