# services.fleetkit: the deploy server (fleetkit serve) on a NixOS host, in
# GitOps mode: it mirrors the estate repo and deploys the branch, on a GitHub
# push webhook or by polling. Experimental (docs/pulumi.md).
#
# The host holds what a deploy needs, so treat it like the operator's machine:
# the age key that decrypts the estate's secrets, the SSH key Colmena deploys
# with, a read-only deploy key for the repo, the Pulumi passphrase, and the API
# token. All of them come from files (environmentFile, or paths in it); none is
# a Nix value, so none reaches the store.
#
# tokens.<name>: API tokens limited to an estate and to what they may run. The
# names and scopes are configuration and are rendered into a file in the store
# (FLEETKIT_API_TOKENS_FILE); a token's value is not: the file names the path
# of a file that holds it (tokenFile), or its SHA-256 digest.
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
  goals = [
    "switch"
    "test"
    "boot"
    "dry-activate"
  ];
  # What the server accepts as a token's name (cli/fleetkit_cli/tokens.py,
  # _NAME and RESERVED): refused here, at evaluation, rather than by a service
  # that does not start.
  reservedNames = [
    "api-token"
    "no-auth"
    "gitops"
  ];
  nameOk = name: builtins.match "[A-Za-z0-9][A-Za-z0-9._-]{0,63}" name != null && !(lib.elem name reservedNames);
  emptyDigest = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855";
  # A string, not a path: a path value would be copied into the store when the
  # file is rendered, and the token with it.
  secretPath =
    types.addCheck types.str (p: lib.hasPrefix "/" p && !lib.hasPrefix builtins.storeDir p)
    // {
      description = "absolute path, as a string, outside the Nix store";
    };
  tokenModule = {
    options = {
      tokenFile = mkOption {
        type = types.nullOr secretPath;
        default = null;
        example = "/run/secrets/fleetkit-token-tenant";
        description = ''
          Path of a file that holds the token's value (one line), readable by
          the fleetkit user when the service starts. A string naming a path
          outside the Nix store: the value never reaches the store. Exactly
          one of tokenFile and sha256 is set.
        '';
      };
      sha256 = mkOption {
        type = types.nullOr (types.strMatching "[0-9a-f]{64}");
        default = null;
        description = ''
          The SHA-256 digest of the token's value, in hex, instead of
          tokenFile: the server then needs no file for this token. Only for a
          value that is long and random (a digest of anything guessable can be
          searched).
        '';
      };
      estates = mkOption {
        type = types.listOf types.str;
        default = [ ];
        example = [ "tenant" ];
        description = ''
          Estates the token may name in a request. It sees the jobs and the
          stacks of these estates and of no other.
        '';
      };
      hives = mkOption {
        type = types.nullOr (types.listOf types.str);
        default = null;
        description = ''
          Hives the token may deploy. A request's hive defaults to its estate
          and must be in this list. null: the same names as estates.
        '';
      };
      infra = mkOption {
        type = types.enum [
          "none"
          "preview"
          "apply"
        ];
        default = "none";
        description = ''
          The Pulumi stage. none: a request must send infra false. preview:
          only in a preview. apply: pulumi up as well.
        '';
      };
      nixos = mkOption {
        type = types.enum [
          "none"
          "build"
          "apply"
        ];
        default = "none";
        description = ''
          The Colmena stage. none: a request must send nixos false. build:
          only in a preview (colmena build). apply: colmena apply with one of
          goals.
        '';
      };
      goals = mkOption {
        type = types.listOf (types.enum goals);
        default = [ "dry-activate" ];
        example = [
          "dry-activate"
          "switch"
        ];
        description = "Colmena goals the token may apply (with nixos = apply).";
      };
      revs = mkOption {
        type = types.enum [
          "deploy-branch"
          "head"
          "any"
        ];
        default = "deploy-branch";
        description = ''
          deploy-branch: the commit of a request, of a preview too, must be on
          the deploy branch or on one of previewBranches (a preview evaluates
          the commit with the server's credentials present). That is any
          commit of their history: the token can deploy an old configuration
          again. head: only the head of the deploy branch, and in a preview
          the head of one of previewBranches too. any: any commit the repo
          has, as for the unscoped token.
        '';
      };
      allow = mkOption {
        type = types.bool;
        default = false;
        description = ''
          Whether a request may carry allow_replace, allow_delete,
          allow_update, allow_create, targets or refresh.
        '';
      };
    };
  };
  tokensFile = pkgs.writeText "fleetkit-tokens.json" (
    builtins.toJSON {
      tokens = lib.mapAttrs (
        _: t:
        (if t.tokenFile != null then { file = t.tokenFile; } else { inherit (t) sha256; })
        // {
          inherit (t)
            estates
            infra
            nixos
            goals
            revs
            allow
            ;
        }
        // lib.optionalAttrs (t.hives != null) { inherit (t) hives; }
      ) cfg.tokens;
    }
  );
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
    tokens = mkOption {
      type = types.attrsOf (types.submodule tokenModule);
      default = { };
      example = lib.literalExpression ''
        {
          tenant-ci = {
            tokenFile = config.sops.secrets."fleetkit/tenant-ci".path;
            estates = [ "tenant" ];
            nixos = "apply";
            goals = [ "dry-activate" "switch" ];
          };
        }
      '';
      description = ''
        Named API tokens, each limited to estates and to what it may run
        (docs/pulumi.md, "Scoped tokens"). The name is what a job records as
        having started it. A token named here can do nothing that its options
        do not grant; the token of FLEETKIT_API_TOKEN (environmentFile) stays
        and can do everything. The server does not start when a tokenFile
        cannot be read or two tokens have the same value.
      '';
    };
    tokensFile = mkOption {
      type = types.path;
      readOnly = true;
      description = ''
        The file tokens is rendered to (FLEETKIT_API_TOKENS_FILE): names,
        scopes, and for each token the path of its tokenFile or its digest.
        No value.
      '';
    };
  };

  config = lib.mkIf cfg.enable {
    services.fleetkit.tokensFile = tokensFile;
    assertions = lib.concatLists (
      lib.mapAttrsToList (name: t: [
        {
          assertion = (t.tokenFile != null) != (t.sha256 != null);
          message = "services.fleetkit.tokens.${name}: exactly one of tokenFile and sha256 must be set.";
        }
        {
          assertion = nameOk name;
          message = "services.fleetkit.tokens: the name ${builtins.toJSON name} is not a token name (letters, digits, '.', '_' and '-', at most 64, starting with a letter or a digit, and not ${lib.concatStringsSep ", " reservedNames}, which are what a job records for the unscoped token and for the server itself).";
        }
        {
          assertion = t.sha256 != emptyDigest;
          message = "services.fleetkit.tokens.${name}: sha256 is the digest of the empty value.";
        }
      ]) cfg.tokens
    );

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
      }
      // lib.optionalAttrs (cfg.tokens != { }) {
        FLEETKIT_API_TOKENS_FILE = "${cfg.tokensFile}";
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
