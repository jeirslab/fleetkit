# fleet.operators: principals (people and service identities), roles,
# grants, breakglass. ids: principal/role/grant = name.
# Principal schema (emails, tailnets, keys.{ssh,gpg,age}).
{ lib, config, ... }:
let
  inherit (lib) types mkOption;
  h = import ./lib.nix { inherit lib; };
  inherit (config.fleet.report) ids;

  # Public keys only. The comment group is single-line, so a private key
  # pasted after a public one never matches; key assertions below also
  # reject PRIVATE KEY and age secret key markers in any public field.
  sshPublicKey = "(ssh-ed25519|ssh-rsa|ecdsa-sha2-nistp[0-9]+|sk-ssh-ed25519@openssh.com) [A-Za-z0-9+/=]+( [^\n]*)?";

  # One key: public material checked per category, plus an optional sops ref
  # to the private half. A bare string is shorthand for { public = <string>; }.
  keyType =
    pattern:
    types.coercedTo types.str (public: { inherit public; }) (
      types.submodule {
        options = {
          public = mkOption { type = types.strMatching pattern; };
          privateRef = mkOption {
            type = types.nullOr types.str; # shape checked by secretRefAssertions (a type error would echo the value)
            default = null;
            description = "sops reference to the private half.";
          };
        };
      }
    );

  keysOf =
    pattern:
    mkOption {
      type = types.attrsOf (keyType pattern);
      default = { };
    };

  principalType = types.submodule (
    { name, ... }:
    {
      options = {
        id = h.mkId name;
        kind = mkOption {
          type = types.enum [
            "person"
            "service"
          ];
        };
        github = mkOption {
          type = types.nullOr types.str;
          default = null;
        };
        emails = mkOption {
          type = types.attrsOf (types.strMatching "[^@ ]+@[^@ ]+");
          default = { };
          description = "Email address by estate id.";
        };
        tailnets = mkOption {
          type = types.attrsOf (
            types.submodule {
              options = {
                user = mkOption { type = types.str; };
                authKeyRef = mkOption {
                  type = types.nullOr types.str;
                  default = null;
                };
              };
            }
          );
          default = { };
          description = "Tailnet membership by tailnet id (for example homelab/personal).";
        };
        keys = mkOption {
          type = types.submodule {
            options = {
              ssh = keysOf sshPublicKey;
              gpg = keysOf "[0-9A-F]{40}";
              age = keysOf "age1[02-9ac-hj-np-z]{58}";
            };
          };
          default = { };
          description = "Public keys by category and label.";
        };
      };
    }
  );

  roleType = types.submodule (
    { name, ... }:
    {
      options = {
        id = h.mkId name;
        account = mkOption {
          type = types.str;
          description = "Unix account the role maps to.";
        };
        can = mkOption {
          type = types.listOf (
            types.enum [
              "login"
              "sudo"
              "deploy"
            ]
          );
        };
      };
    }
  );

  grantType = types.submodule (
    { name, ... }:
    {
      options = {
        id = h.mkId name;
        principals = mkOption {
          type = types.listOf types.str;
          description = "Principal ids.";
        };
        role = mkOption {
          type = types.str;
          description = "Role id (the role name).";
        };
        where = mkOption {
          type = types.nullOr (
            types.submodule {
              options = {
                regions = mkOption {
                  type = types.listOf types.str;
                  default = [ ];
                  description = "Site regions the grant applies to.";
                };
                estates = mkOption {
                  type = types.listOf types.str;
                  default = [ ];
                  description = "Estate ids the grant applies to.";
                };
              };
            }
          );
          default = null;
          description = "Scope; null = everywhere.";
        };
      };
    }
  );

  breakglassType = types.submodule {
    options.method = mkOption {
      type = types.enum [ "console" ];
    };
  };

  regions = lib.unique (lib.mapAttrsToList (_: s: s.region) config.fleet.sites);

  grantAssertions = lib.concatLists (
    lib.mapAttrsToList (
      g: grant:
      let
        w = "fleet.operators.grants.${g}";
      in
      h.refAssertions {
        where = "${w}.principals";
        kind = "principal";
        ids = ids.principal;
      } grant.principals
      ++ [
        (h.refAssertion {
          where = "${w}.role";
          kind = "role";
          ids = ids.role;
        } grant.role)
      ]
      ++ lib.optionals (grant.where != null) (
        h.refAssertions {
          where = "${w}.where.estates";
          kind = "estate";
          ids = ids.estate;
        } grant.where.estates
        ++ h.refAssertions {
          where = "${w}.where.regions";
          kind = "region";
          ids = regions;
        } grant.where.regions
      )
    ) config.fleet.operators.grants
  );

  principalAssertions = lib.concatLists (
    lib.mapAttrsToList (
      p: principal:
      let
        w = "fleet.operators.principals.${p}";
        keyAssertions = lib.concatLists (
          lib.mapAttrsToList (
            cat: keys:
            lib.concatLists (
              lib.mapAttrsToList (
                n: key:
                [
                  {
                    assertion =
                      !(lib.hasInfix "PRIVATE KEY" key.public || lib.hasInfix ("AGE-SECRET-" + "KEY") key.public);
                    message = "${w}.keys.${cat}.${n}.public: contains private key material (value withheld); public fields hold public keys only";
                  }
                ]
                ++ lib.optionals (key.privateRef != null) (
                  h.secretRefAssertions {
                    where = "${w}.keys.${cat}.${n}.privateRef";
                    ids = ids.secretRef;
                  } key.privateRef
                  ++ lib.optional (principal.kind == "person") {
                    assertion = false;
                    message = "${w}.keys.${cat}.${n}.privateRef: personal keys never carry a privateRef (principal kind is person)";
                  }
                )
              ) keys
            )
          ) principal.keys
        );
      in
      map (
        e:
        h.refAssertion {
          where = "${w}.emails";
          kind = "estate";
          ids = ids.estate;
        } e
      ) (builtins.attrNames principal.emails)
      ++ lib.concatMap (
        t:
        [
          (h.refAssertion {
            where = "${w}.tailnets";
            kind = "tailnet";
            ids = ids.tailnet;
          } t)
        ]
        ++ lib.optionals (principal.tailnets.${t}.authKeyRef != null) (
          h.secretRefAssertions {
            where = "${w}.tailnets.${t}.authKeyRef";
            ids = ids.secretRef;
            estate = builtins.head (lib.splitString "/" t);
          } principal.tailnets.${t}.authKeyRef
        )
      ) (builtins.attrNames principal.tailnets)
      ++ keyAssertions
    ) config.fleet.operators.principals
  );

  breakglassAssertions = lib.mapAttrsToList (
    e: _:
    h.refAssertion {
      where = "fleet.operators.breakglass.${e}";
      kind = "estate";
      ids = ids.estate;
    } e
  ) config.fleet.operators.breakglass;
in
{
  options.fleet.operators = {
    principals = mkOption {
      type = types.lazyAttrsOf principalType;
      default = { };
    };
    roles = mkOption {
      type = types.lazyAttrsOf roleType;
      default = { };
    };
    grants = mkOption {
      type = types.lazyAttrsOf grantType;
      default = { };
    };
    breakglass = mkOption {
      type = types.lazyAttrsOf breakglassType;
      default = { };
      description = "Per-estate breakglass access, keyed by estate name.";
    };
  };

  config.assertions = principalAssertions ++ grantAssertions ++ breakglassAssertions;
}
