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

  config.assertions = estateKeys ++ perRepo ++ duplicates;
}
