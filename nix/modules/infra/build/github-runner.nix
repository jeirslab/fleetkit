{ config, lib, pkgs, ... }:

# GitHub Actions self-hosted runner on the builder — the push-to-main CD path
# whose secrets never leave the estate.
#
# The deploy MUST run on-prem: a GitHub-hosted runner has no route to the
# internal net and no SOPS key. So instead of shipping the estate's master age
# key + a root deploy key into GitHub secrets (a catastrophic-compromise SPOF),
# this registers a self-hosted runner HERE. GitHub is only the trigger and the
# dashboard; execution + credentials stay on the builder. The runner picks up a
# workflow that runs `fleet deploy … --wait` (honest exit code), so a red
# workflow means a failed deploy — not "the tmux session launched".
#
# Ephemeral by default (needs a PAT): a fresh runner per job, de-registered and
# wiped after one job — no lingering runner state between deploys.
#
# Identity, as with the poll runner (infra.build.deployRunner): the `user` must
# already hold what a hand-run deploy needs — the SOPS age key (point
# `sopsAgeKeyFile` at it), a git credential, and SSH to the targets. Workflow
# code runs as this user, so scope it deliberately.
#
# The matching workflow the consumer commits to its repo (.github/workflows/):
#
#   name: deploy
#   on: { push: { branches: [ main ] } }
#   concurrency: { group: deploy, cancel-in-progress: false }
#   jobs:
#     deploy:
#       runs-on: [ self-hosted, fleet-deploy ]   # matches `labels`
#       steps:
#         - uses: actions/checkout@v4
#         - run: nix develop --command fleet deploy nixos apply all --wait
#
# `apply all` is synchronous; a per-host command must carry --wait. Because the
# runner user holds the SOPS key + SSH, `nix develop` gives the exact toolchain
# (colmena, tofu, sops, fleet) from the checked-out flake.

let
  inherit (lib) mkEnableOption mkOption mkIf types;
  cfg = config.infra.build.githubRunner;
in
{
  options.infra.build.githubRunner = {
    enable = mkEnableOption "GitHub Actions self-hosted runner on the builder for push-to-main CD";

    url = mkOption {
      type = types.str;
      example = "https://github.com/example/fleet";
      description = ''
        Repository (or org) URL the runner registers with. A per-repo PAT
        wants the repo URL; an org-wide PAT wants the org URL (…/example, not
        …/example/fleet).
      '';
    };

    tokenFile = mkOption {
      type = types.path;
      example = lib.literalExpression ''config.sops.secrets."github/runner-pat".path'';
      description = ''
        Absolute path to a file holding a fine-grained PAT with read+write on
        the repo/org self-hosted runners (one line, no trailing newline).
        Wire it from SOPS — the token never belongs in the Nix store. A PAT
        (not a registration token) is required because the runner is ephemeral
        and re-registers per job.
      '';
    };

    name = mkOption {
      type = types.nullOr types.str;
      default = null;
      description = "Runner display name registered with GitHub. null = the host's name.";
    };

    labels = mkOption {
      type = types.listOf types.str;
      default = [ "fleet-deploy" ];
      description = "Extra runner labels. The deploy workflow targets these via `runs-on: [self-hosted, …]`.";
    };

    user = mkOption {
      type = types.str;
      default = "root";
      description = ''
        User the runner (and therefore the workflow's deploy) runs as. Must
        already hold the SOPS age key, a git credential for `url`, and SSH to
        the deploy targets. Workflow code executes as this user, so choose it
        deliberately — root is the simplest on a single-purpose builder, a
        dedicated deploy user is tighter.
      '';
    };

    sopsAgeKeyFile = mkOption {
      type = types.nullOr types.str;
      default = null;
      example = "/var/lib/fleet-deploy/age.key";
      description = ''
        Path to the age key exported as SOPS_AGE_KEY_FILE for workflow runs, so
        `fleet deploy` can decrypt the estate's secrets. null leaves the runner
        env untouched (rely on the user's own SOPS_AGE_KEY_FILE / default key
        location).
      '';
    };

    ephemeral = mkOption {
      type = types.bool;
      default = true;
      description = ''
        Register a fresh runner per job and wipe it afterward (requires a PAT
        in `tokenFile`). Safer default — no runner state persists between
        deploys. Set false only if you must use a registration token.
      '';
    };

    extraPackages = mkOption {
      type = types.listOf types.package;
      default = [ ];
      example = lib.literalExpression "[ pkgs.colmena pkgs.opentofu ]";
      description = ''
        Extra packages on the runner PATH. Usually unnecessary — the workflow's
        `nix develop` provides the toolchain — but available for workflows that
        call tools before entering the devshell.
      '';
    };

    workDir = mkOption {
      type = types.nullOr types.str;
      default = null;
      description = "Runner working directory (GITHUB_WORKSPACE); wiped on each service start. null = the systemd RuntimeDirectory.";
    };
  };

  config = mkIf cfg.enable {
    services.github-runners.fleet-deploy = {
      enable = true;
      inherit (cfg) url tokenFile name ephemeral workDir user;
      extraLabels = cfg.labels;
      replace = true;
      # git + nix are enough; the workflow's `nix develop` brings colmena /
      # tofu / sops / fleet from the checked-out flake.
      extraPackages = [ pkgs.git pkgs.nix ] ++ cfg.extraPackages;
      extraEnvironment = lib.optionalAttrs (cfg.sopsAgeKeyFile != null) {
        SOPS_AGE_KEY_FILE = cfg.sopsAgeKeyFile;
      };
    };
  };
}
