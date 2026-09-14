{ config, lib, pkgs, ... }:

# GitOps CD runner (the original motivation for the deployer work): push to a
# branch → the fleet deploys itself. Runs ON the builder host as a systemd
# timer that polls a git branch and, when its tip moves, checks the repo out
# and runs a deploy that BLOCKS on the real result (`fleet … --wait` / the
# already-synchronous `apply all`). Idempotence is a `.deployed-ref` marker
# next to the checkout: a tick whose remote tip already equals the marker is a
# no-op, so the deploy only fires on an actual new commit — or a retry after a
# failed one (the marker is written only on success).
#
# Poll, not webhook: no inbound port, no shared secret, tailnet-friendly — the
# right default for a self-hosted estate. The trigger is deliberately dumb; the
# intelligence (what changed, which stacks) lives in the deploy command, which
# a consumer overrides via `deployCommand`.
#
# This module ships the mechanism; the consumer wires identity: the runner
# `user` must already hold what a hand-run deploy needs — the SOPS age key, a
# git credential for `repoUrl`, and SSH access to the targets. The runner adds
# no privilege of its own.

let
  inherit (lib) mkEnableOption mkOption mkIf types;
  cfg = config.infra.build.deployRunner;

  runScript = pkgs.writeShellApplication {
    name = "fleet-deploy-runner";
    runtimeInputs = [ pkgs.git pkgs.nix cfg.fleetPackage ] ++ cfg.extraPackages;
    text = ''
      set -euo pipefail
      dir=${lib.escapeShellArg cfg.workingDir}
      branch=${lib.escapeShellArg cfg.branch}
      marker="$dir/.deployed-ref"

      if [ ! -d "$dir/.git" ]; then
        echo "cloning ${cfg.repoUrl} ($branch) into $dir"
        mkdir -p "$(dirname "$dir")"
        git clone --branch "$branch" ${lib.escapeShellArg cfg.repoUrl} "$dir"
      fi
      cd "$dir"

      git fetch --quiet origin "$branch"
      remote="$(git rev-parse "origin/$branch")"

      if [ -f "$marker" ] && [ "$(cat "$marker")" = "$remote" ]; then
        echo "up to date: $branch @ $remote already deployed; nothing to do"
        exit 0
      fi

      echo "new tip on $branch: $remote — checking out + deploying"
      git reset --quiet --hard "$remote"

      # The deploy blocks on the real result (apply all is synchronous; a
      # per-host command should carry --wait). Marker is written ONLY on
      # success, so a failed deploy retries on the next tick.
      if ${lib.escapeShellArgs cfg.deployCommand}; then
        printf '%s\n' "$remote" > "$marker"
        echo "deployed $branch @ $remote"
      else
        rc=$?
        echo "deploy FAILED (rc=$rc) for $branch @ $remote — will retry next tick"
        exit "$rc"
      fi
    '';
  };
in
{
  options.infra.build.deployRunner = {
    enable = mkEnableOption "GitOps CD runner: poll a branch and deploy the fleet on new commits";

    repoUrl = mkOption {
      type = types.str;
      example = "git@github.com:example/fleet.git";
      description = ''
        Git URL of the fleet repository to watch. The runner `user` must be
        able to clone/fetch it non-interactively (deploy key / credential
        helper already configured for this user).
      '';
    };

    branch = mkOption {
      type = types.str;
      default = "main";
      description = "Branch whose tip triggers a deploy when it moves.";
    };

    workingDir = mkOption {
      type = types.str;
      default = "/var/lib/fleet-deploy-runner/checkout";
      description = "Where the fleet repo is checked out and deploys run from.";
    };

    interval = mkOption {
      type = types.str;
      default = "*:0/5";
      example = "hourly";
      description = "systemd OnCalendar cadence for the branch poll.";
    };

    fleetPackage = mkOption {
      type = types.package;
      example = lib.literalExpression "fleetkit.packages.\${system}.fleet";
      description = ''
        The `fleet` CLI package the runner invokes. A NixOS module cannot
        reference fleetkit's own flake output, so the consumer passes it in
        (e.g. from the fleetkit flake input).
      '';
    };

    deployCommand = mkOption {
      type = types.listOf types.str;
      default = [ "fleet" "deploy" "nixos" "apply" "all" ];
      example = lib.literalExpression ''[ "fleet" "deploy" "nixos" "apply" "host" "web" "db" "--wait" ]'';
      description = ''
        Command run (in `workingDir`) on a new commit. Must block on the real
        result and exit non-zero on failure — `apply all` is synchronous; a
        per-host command must pass `--wait`. Override to sequence tofu +
        colmena, restrict to changed stacks, etc.
      '';
    };

    user = mkOption {
      type = types.str;
      default = "root";
      description = ''
        User the poll + deploy run as. Must already hold what a hand-run deploy
        needs: the SOPS age key, a git credential for `repoUrl`, and SSH access
        to the targets. The runner grants no privilege of its own.
      '';
    };

    extraPackages = mkOption {
      type = types.listOf types.package;
      default = [ ];
      example = lib.literalExpression "[ pkgs.colmena pkgs.opentofu pkgs.sops ]";
      description = ''
        Extra packages on the runner's PATH. Usually unnecessary — `fleet`
        re-execs into the checked-out repo's devshell for the toolchain — but
        available for deploys that call tools directly.
      '';
    };
  };

  config = mkIf cfg.enable {
    systemd.services.fleet-deploy-runner = {
      description = "GitOps CD: poll ${cfg.branch} and deploy the fleet on new commits";
      # Deploys reach the network (git, SSH to targets, cache); order after it.
      after = [ "network-online.target" ];
      wants = [ "network-online.target" ];
      serviceConfig = {
        Type = "oneshot";
        User = cfg.user;
        ExecStart = lib.getExe runScript;
      };
    };

    systemd.timers.fleet-deploy-runner = {
      description = "Poll ${cfg.branch} for fleet deploys (${cfg.interval})";
      wantedBy = [ "timers.target" ];
      timerConfig = {
        OnCalendar = cfg.interval;
        OnBootSec = "2min";
        Persistent = true;
      };
    };
  };
}
