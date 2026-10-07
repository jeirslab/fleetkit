# fleet.repos.<estate>.<name>: git repositories; id "<estate>/<name>".
{ lib, config, ... }:
let
  inherit (lib) types mkOption;
  h = import ./lib.nix { inherit lib; };
  inherit (config.fleet.report) ids;

  boolOpt = mkOption { type = types.bool; };
  nullable = type: mkOption {
    type = types.nullOr type;
    default = null;
  };

  repoType =
    estate:
    types.submodule (
      { name, ... }:
      {
        options = {
          id = h.mkId (h.qualify estate name);
          name = nullable types.str // {
            description = "GitHub repository name when it differs from the attribute name.";
          };
          visibility = mkOption {
            type = types.enum [
              "public"
              "private"
              "internal"
            ];
          };
          description = nullable types.str;
          defaultBranch = nullable types.str;
          features = nullable (
            types.submodule {
              options = {
                issues = boolOpt;
                wiki = boolOpt;
                projects = boolOpt;
              };
            }
          );
          merge = nullable (
            types.submodule {
              options = {
                commit = boolOpt;
                squash = boolOpt;
                rebase = boolOpt;
                deleteBranchOnMerge = boolOpt;
              };
            }
          );
          archiveOnDestroy = mkOption {
            type = types.nullOr types.bool;
            default = null;
            description = "github_repository.archive_on_destroy: archive the repository instead of deleting it when it leaves the configuration. null = true, the safe choice; say false to allow deletion.";
          };
          fork = mkOption {
            type = types.bool;
            default = false;
          };
          archived = mkOption {
            type = types.bool;
            default = false;
          };
          environments = mkOption {
            type = types.lazyAttrsOf (
              types.submodule {
                options = {
                  estateEnvironment = mkOption {
                    type = types.str;
                    description = "Environment id (<estate>/<env>) of the same estate.";
                  };
                  branches = mkOption { type = types.listOf types.str; };
                };
              }
            );
            default = { };
          };
          actions = mkOption {
            type = types.submodule {
              options = {
                secrets = mkOption {
                  type = types.lazyAttrsOf (
                    types.submodule {
                      options.sourceRef = mkOption {
                        type = types.str;
                        description = "github_actions_secret.plaintext_value: a sops reference (sops:<estate>/<file>#<key>) of the same estate, rendered as a data.sops_file reference, never a literal.";
                      };
                    }
                  );
                  default = { };
                  description = "Repository-level Actions secrets by name (github_actions_secret).";
                };
                variables = mkOption {
                  type = types.lazyAttrsOf types.str;
                  default = { };
                  description = "Repository-level Actions variables, name = value (github_actions_variable).";
                };
              };
            };
            default = { };
          };
          labels = mkOption {
            type = types.lazyAttrsOf (
              types.submodule {
                options = {
                  color = mkOption {
                    type = types.str;
                    description = "github_issue_label.color: six hex digits, no leading #.";
                  };
                  description = nullable types.str;
                };
              }
            );
            default = { };
            description = "Issue labels by name (github_issue_label, one resource per label).";
          };
          files = mkOption {
            type = types.lazyAttrsOf (
              types.submodule {
                options = {
                  content = mkOption {
                    type = types.str;
                    description = "github_repository_file.content: the file text, supplied by the estate.";
                  };
                  branch = nullable types.str // {
                    description = "github_repository_file.branch; null = the repository's default branch.";
                  };
                  message = nullable types.str // {
                    description = "github_repository_file.commit_message; null = a generated message.";
                  };
                  overwrite = mkOption {
                    type = types.bool;
                    default = true;
                    description = "github_repository_file.overwrite_on_create.";
                  };
                };
              }
            );
            default = { };
            description = "Files kept in the repository by path (github_repository_file), e.g. a workflow file.";
          };
          runners = mkOption {
            type = types.lazyAttrsOf (
              types.submodule {
                options = {
                  labels = mkOption {
                    type = types.listOf types.str;
                    description = "The labels the runner registers with, which a workflow's runs-on selects (for example [ \"self-hosted\" \"checks\" ]). At least one. Reported in locals.fleet_runners.";
                  };
                  on = mkOption {
                    type = types.str;
                    description = "Guest id (<estate>/<name>) that runs this runner. It must be a declared guest of the same estate as the repository: a runner on another estate's guest is an evaluation error. Model data only; Terraform cannot register a runner.";
                  };
                };
              }
            );
            default = { };
          };
          deployKeys = mkOption {
            type = types.lazyAttrsOf (
              types.submodule {
                options.readOnly = boolOpt;
              }
            );
            default = { };
          };
        };
      }
    );

  # repos.<estate> is a container of typed repos, not a loose block.
  estateReposType = types.submodule (
    { name, ... }:
    {
      freeformType = types.lazyAttrsOf (repoType name);
    }
  );

  inherit (config.fleet) repos;

  perRepo = lib.concatLists (
    lib.mapAttrsToList (
      estate: rs:
      lib.concatLists (
        lib.mapAttrsToList (
          n: r:
          lib.concatLists (
            lib.mapAttrsToList (
              env: e:
              let
                where = "fleet.repos.${estate}.${n}.environments.${env}.estateEnvironment";
              in
              [
                (h.refAssertion {
                  inherit where;
                  kind = "environment";
                  ids = ids.environment;
                } e.estateEnvironment)
                {
                  assertion = lib.hasPrefix "${estate}/" e.estateEnvironment;
                  message = "${where}: environment \"${e.estateEnvironment}\" does not belong to estate \"${estate}\"";
                }
              ]
            ) r.environments
          )
        ) rs
      )
    ) repos
  );

  # Repo-level Actions secrets, variables, labels, files and runners.
  # GitHub compares Actions secret and variable names without regard to case,
  # so the reserved prefix and duplicates are checked the same way.
  nameOk =
    n: builtins.match "[A-Za-z_][A-Za-z0-9_]*" n != null && !(lib.hasPrefix "GITHUB_" (lib.toUpper n));
  caseClashes =
    names:
    lib.attrValues (
      lib.filterAttrs (_: v: builtins.length v > 1) (lib.groupBy lib.toUpper names)
    );
  perRepoCase = lib.concatLists (
    lib.mapAttrsToList (
      estate: rs:
      lib.concatLists (
        lib.mapAttrsToList (
          n: r:
          lib.concatMap
            (
              kind:
              map (clash: {
                assertion = false;
                message = "fleet.repos.${estate}.${n}.actions.${kind}: ${
                  lib.concatMapStringsSep ", " (x: "\"${x}\"") clash
                } are the same name on GitHub (names differ only in case)";
              }) (caseClashes (lib.attrNames r.actions.${kind}))
            )
            [
              "secrets"
              "variables"
            ]
        ) rs
      )
    ) config.fleet.repos
  );
  perRepoGithub = lib.concatLists (
    lib.mapAttrsToList (
      estate: rs:
      lib.concatLists (
        lib.mapAttrsToList (
          n: r:
          let
            w = p: "fleet.repos.${estate}.${n}.${p}";
          in
          lib.concatLists (
            lib.mapAttrsToList (
              s: sv:
              [
                {
                  assertion = nameOk s;
                  message = "${w "actions.secrets.${s}"}: \"${s}\" is not a valid Actions secret name (letters, digits and underscores, not starting with a digit, not starting with GITHUB_)";
                }
              ]
              ++ h.secretRefAssertions {
                where = w "actions.secrets.${s}.sourceRef";
                ids = ids.secretRef;
                inherit estate;
              } sv.sourceRef
            ) r.actions.secrets
          )
          ++ lib.mapAttrsToList (v: _: {
            assertion = nameOk v;
            message = "${w "actions.variables.${v}"}: \"${v}\" is not a valid Actions variable name (letters, digits and underscores, not starting with a digit, not starting with GITHUB_)";
          }) r.actions.variables
          ++ lib.mapAttrsToList (l: lv: {
            assertion = builtins.match "[0-9A-Fa-f]{6}" lv.color != null;
            message = "${w "labels.${l}.color"}: \"${lv.color}\" is not six hex digits without a leading #";
          }) r.labels
          ++ lib.concatLists (
            lib.mapAttrsToList (
              f: _:
              [
                {
                  assertion = f != "" && !(lib.hasPrefix "/" f) && !(builtins.elem ".." (lib.splitString "/" f));
                  message = "${w "files.${f}"}: file path \"${f}\" must be relative to the repository root and contain no ..";
                }
              ]
            ) r.files
          )
          ++ lib.concatLists (
            lib.mapAttrsToList (
              rn: rv:
              [
                (h.refAssertion {
                  where = w "runners.${rn}.on";
                  kind = "guest";
                  ids = ids.guest;
                } rv.on)
                # A declared guest, and one of the repository's own estate.
                # An id that is no guest at all is reported above only.
                {
                  assertion = !(builtins.elem rv.on ids.guest) || lib.hasPrefix "${estate}/" rv.on;
                  message = "${w "runners.${rn}.on"}: guest reference \"${rv.on}\" belongs to another estate (a runner must be on a guest of the repository's estate, expected ${estate}/...)";
                }
                {
                  assertion = rv.labels != [ ];
                  message = "${w "runners.${rn}.labels"}: a runner needs at least one label";
                }
              ]
            ) r.runners
          )
        ) rs
      )
    ) repos
  );

  estateKeys = lib.mapAttrsToList (
    estate: _:
    h.refAssertion {
      where = "fleet.repos.${estate}";
      kind = "estate";
      ids = ids.estate;
    } estate
  ) repos;

  # Duplicate effective GitHub names within one estate.
  duplicates = lib.concatLists (
    lib.mapAttrsToList (
      estate: rs:
      let
        byName = lib.groupBy (x: x.eff) (
          lib.mapAttrsToList (n: r: {
            inherit n;
            eff = if r.name != null then r.name else n;
          }) rs
        );
      in
      lib.mapAttrsToList (eff: xs: {
        assertion = builtins.length xs == 1;
        message = "fleet.repos.${estate}: GitHub repository name \"${eff}\" is used by more than one repo (${lib.concatMapStringsSep ", " (x: x.n) xs})";
      }) byName
    ) repos
  );
in
{
  options.fleet.repos = mkOption {
    type = types.attrsOf estateReposType;
    default = { };
  };

  config.assertions = estateKeys ++ perRepo ++ perRepoGithub ++ perRepoCase ++ duplicates;
}
