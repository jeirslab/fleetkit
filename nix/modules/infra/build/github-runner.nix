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

    app = {
      id = mkOption {
        type = types.nullOr types.str;
        default = null;
        example = "5027214";
        description = ''
          Register through the organization's GitHub App instead of a PAT. The App
          needs the organization permission "Self-hosted runners: read & write" and
          must be installed on `url`'s organization. Takes priority over `tokenFile`:
          before every start (so before every ephemeral re-registration) a oneshot
          unit mints an installation token from the App key and exchanges it for a
          one-hour registration token. No long-lived credential exists on the host.
        '';
      };
      privateKeyFile = mkOption {
        type = types.nullOr types.path;
        default = null;
        description = "The App's private key (PEM), provided by the secrets store at runtime. Never a path in the Nix store.";
      };
    };
    tokenFile = mkOption {
      type = types.nullOr types.path;
      default = null;
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

  config = mkIf cfg.enable (
    let
      useApp = cfg.app.id != null;
      # The org from the runner URL (https://github.com/<org>); a repository URL
      # keeps its owner, which is also what the App's installation is keyed by.
      org = builtins.head (lib.splitString "/" (lib.removePrefix "https://github.com/" cfg.url));
      tokenDir = "/run/github-runner-token";
      mintedToken = "${tokenDir}/fleet-deploy";
      mint = pkgs.writeShellApplication {
        name = "github-runner-app-token";
        runtimeInputs = with pkgs; [ coreutils openssl curl jq ];
        text = ''
          # App JWT (RS256, 9 minutes) -> installation token for the org ->
          # runner registration token (1 h) -> the file the runner unit reads.
          b64() { openssl base64 -A | tr '+/' '-_' | tr -d '='; }
          now=$(date +%s)
          header=$(printf '{"alg":"RS256","typ":"JWT"}' | b64)
          payload=$(printf '{"iat":%d,"exp":%d,"iss":"%s"}' "$((now - 60))" "$((now + 540))" "${cfg.app.id}" | b64)
          sig=$(printf '%s.%s' "$header" "$payload" | openssl dgst -sha256 -sign "${cfg.app.privateKeyFile}" | b64)
          jwt="$header.$payload.$sig"
          # Name the step in the failure: a 403 from the registration endpoint is
          # "the App lacks Self-hosted runners: write"; from /app/installations it
          # is a bad key or id. Both read the same without this.
          step=""
          api() { curl -fsS -H "Accept: application/vnd.github+json" -H "X-GitHub-Api-Version: 2022-11-28" "$@" || { echo "github-runner: $step failed (HTTP error above)" >&2; exit 1; }; }
          step="list App installations (bad App id or private key?)"
          inst=$(api -H "Authorization: Bearer $jwt" https://api.github.com/app/installations \
                 | jq -r --arg org "${org}" '.[] | select(.account.login == $org) | .id' | head -n1)
          if [ -z "$inst" ]; then
            echo "github-runner: App ${cfg.app.id} is not installed on ${org}" >&2; exit 1
          fi
          step="mint an installation token for ${org}"
          itok=$(api -X POST -H "Authorization: Bearer $jwt" \
                 "https://api.github.com/app/installations/$inst/access_tokens" | jq -r .token)
          step="mint a runner registration token for ${org} (does the App have Self-hosted runners: write, accepted on the installation?)"
          rtok=$(api -X POST -H "Authorization: Bearer $itok" \
                 "https://api.github.com/orgs/${org}/actions/runners/registration-token" | jq -r .token)
          if [ -z "$rtok" ] || [ "$rtok" = null ]; then
            echo "github-runner: could not mint a registration token for ${org} (does the App have 'Self-hosted runners: write'?)" >&2; exit 1
          fi
          install -d -m 0700 "${tokenDir}"
          umask 077; printf '%s' "$rtok" > "${mintedToken}.tmp" && mv "${mintedToken}.tmp" "${mintedToken}"
        '';
      };
      # The same App, as a token any job on this host may use to read the org's
      # own repositories (the private engine, above all): prints an installation
      # token for the runner URL's organization. The org workflows call it on a
      # self-hosted runner so a consumer needs no ORG_APP_PRIVATE_KEY secret.
      orgAppToken = pkgs.writeShellApplication {
        name = "org-app-token";
        runtimeInputs = with pkgs; [ coreutils openssl curl jq ];
        text = ''
          b64() { openssl base64 -A | tr '+/' '-_' | tr -d '='; }
          now=$(date +%s)
          header=$(printf '{"alg":"RS256","typ":"JWT"}' | b64)
          payload=$(printf '{"iat":%d,"exp":%d,"iss":"%s"}' "$((now - 60))" "$((now + 540))" "${cfg.app.id}" | b64)
          sig=$(printf '%s.%s' "$header" "$payload" | openssl dgst -sha256 -sign "${cfg.app.privateKeyFile}" | b64)
          jwt="$header.$payload.$sig"
          api() { curl -fsS -H "Accept: application/vnd.github+json" -H "X-GitHub-Api-Version: 2022-11-28" "$@"; }
          inst=$(api -H "Authorization: Bearer $jwt" https://api.github.com/app/installations \
                 | jq -r --arg org "${org}" '.[] | select(.account.login == $org) | .id' | head -n1)
          [ -n "$inst" ] || { echo "org-app-token: App ${cfg.app.id} is not installed on ${org}" >&2; exit 1; }
          api -X POST -H "Authorization: Bearer $jwt" "https://api.github.com/app/installations/$inst/access_tokens" | jq -r .token
        '';
      };
    in
    {
    environment.systemPackages = lib.optional useApp orgAppToken;
    assertions = [
      {
        assertion = useApp || cfg.tokenFile != null;
        message = "infra.build.githubRunner: set either app.{id,privateKeyFile} or tokenFile.";
      }
      {
        assertion = !useApp || cfg.app.privateKeyFile != null;
        message = "infra.build.githubRunner: app.id is set but app.privateKeyFile is not.";
      }
    ];

    # Fresh registration token before EVERY start of the runner unit: a
    # oneshot without RemainAfterExit is inactive once done, so a dependent
    # unit's next start runs it again -- which is exactly the ephemeral
    # re-registration cadence.
    systemd.services.github-runner-fleet-deploy-token = mkIf useApp {
      description = "Mint a GitHub Actions runner registration token from the org App";
      wants = [ "network-online.target" ];
      after = [ "network-online.target" ];
      serviceConfig = {
        Type = "oneshot";
        ExecStart = "${mint}/bin/github-runner-app-token";
      };
    };
    systemd.services.github-runner-fleet-deploy = mkIf useApp {
      requires = [ "github-runner-fleet-deploy-token.service" ];
      after = [ "github-runner-fleet-deploy-token.service" ];
    };

    services.github-runners.fleet-deploy = {
      enable = true;
      inherit (cfg) url name ephemeral workDir user;
      tokenFile = if useApp then mintedToken else cfg.tokenFile;
      extraLabels = cfg.labels;
      replace = true;
      # git + nix are enough; the workflow's `nix develop` brings colmena /
      # tofu / sops / fleet from the checked-out flake.
      extraPackages = [ pkgs.git pkgs.nix ] ++ cfg.extraPackages;
      extraEnvironment = lib.optionalAttrs (cfg.sopsAgeKeyFile != null) {
        SOPS_AGE_KEY_FILE = cfg.sopsAgeKeyFile;
      };
    };
  });
}
