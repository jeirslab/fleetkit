# The Pulumi.nix module system: an estate repo declares its Pulumi stacks as
# Nix modules, and each stack evaluates to one Pulumi YAML program (JSON) in
# the store. Evaluation only; fleetkit's runner deploys what this produces.
#
#   stacks.<name> = {
#     estate = "homelab";                       # what `fleetkit deploy <estate>` runs
#     packages.<pkg> = { source; version; parameters; };
#     resources.<key> = { type; name?; properties; options; adopt?; };
#     variables.<key> = { "fn::invoke" = ...; };
#     outputs.<key> = ...;
#     backend = { type = "postgres" | "s3" | "local"; ... };
#   }
#
# Read-only, per stack: `program` (the checked program as an attrset), `file`
# (it as Pulumi.yaml in the store, from builtins.toFile), `secrets` (the sops
# files its invokes read, relative to the repo), `adoptIds` and
# `adoptUnresolved`.
#
# Adoption. resources.<key>.adopt is the provider id of the existing resource
# the declaration describes. It is data beside the program: `adoptIds` is
# { <key> = "<id>"; } for every resource that has one, `fleetkit adopt` imports
# those into the stack's state once, and no program ever carries `import`
# (options.import is refused; a check on the rendered program holds the line).
# An import left in a program destroyed an adopted container on the next `up`
# (fleetkit#61). `adoptUnresolved` is { <key> = "<why>"; } for the resources
# whose id the model does not determine.
#
# Every resource's properties are checked against the pinned Pulumi schema of
# its type (./types.nix): a misspelt property, a value of the wrong type or a
# list where an object goes fails `nix eval` here, at the property's path.
{ lib, ... }:
let
  inherit (lib) types mkOption;
  pt = import ./types.nix { inherit lib; };

  sopsValue = types.submodule {
    options = {
      path = mkOption {
        type = types.str;
        description = "The sops file, relative to the estate repo (or a secret root).";
      };
      extract = mkOption {
        type = types.listOf types.str;
        description = "The key path inside the file.";
      };
    };
  };

  backendType = types.submodule {
    options = {
      type = mkOption {
        type = types.enum [
          "postgres"
          "s3"
          "local"
        ];
        description = "postgres:// (url from urlSecret), s3:// (url, credentials in env) or a local directory.";
      };
      url = mkOption {
        type = types.nullOr types.str;
        default = null;
        description = "s3: the bucket URL. Never a secret: it goes to the store.";
      };
      urlSecret = mkOption {
        type = types.nullOr sopsValue;
        default = null;
        description = "postgres: where the connection string is (it carries the password).";
      };
      urlParams = mkOption {
        type = types.attrsOf types.str;
        default = { };
        description = "postgres: query parameters added to the connection string (search_path, sslmode).";
      };
      env = mkOption {
        type = types.attrsOf sopsValue;
        default = { };
        description = "Environment variables the backend reads, from sops (AWS_ACCESS_KEY_ID, ...).";
      };
      path = mkOption {
        type = types.nullOr types.str;
        default = null;
        description = "local: the state directory; relative to the runner's state directory.";
      };
    };
  };

  resourceModule =
    { name, ... }:
    {
      options = {
        type = mkOption {
          type = types.str;
          description = "The Pulumi type token, e.g. proxmox:index/virtualEnvironmentContainer:VirtualEnvironmentContainer.";
        };
        name = mkOption {
          type = types.str;
          default = name;
          description = "The logical name (the URN's last part); defaults to the key.";
        };
        properties = mkOption {
          type = types.attrsOf types.raw;
          default = { };
          description = "Input properties; checked against the pinned schema of `type`.";
        };
        options = mkOption {
          type = types.submodule {
            freeformType = types.attrsOf types.raw;
            options = {
              provider = mkOption {
                type = types.nullOr types.str;
                default = null;
                description = "\${<provider resource key>}.";
              };
              protect = mkOption {
                type = types.nullOr types.bool;
                default = null;
                description = "Refuse to delete.";
              };
              ignoreChanges = mkOption {
                type = types.nullOr (types.listOf types.str);
                default = null;
                description = "Property paths whose drift is ignored.";
              };
              dependsOn = mkOption {
                type = types.nullOr (types.listOf types.str);
                default = null;
                description = "\${<resource key>} references.";
              };
            };
          };
          default = { };
          description = "Pulumi resource options. `import` is not one: see `adopt`.";
        };
        adopt = mkOption {
          type = types.nullOr types.str;
          default = null;
          description = "The provider id of the existing resource this declaration describes; used once by `fleetkit adopt`, never rendered into the program.";
        };
        adoptUnresolved = mkOption {
          type = types.nullOr types.str;
          default = null;
          description = "Why `adopt` is not known from the model, and what value it needs; listed in the stack's adoptUnresolved while `adopt` is null.";
        };
      };
    };

  prune = lib.filterAttrs (_: v: v != null && v != { });

  stackModule =
    { name, config, ... }:
    let
      stack = name;
      # options.import is refused wherever a resource is read (the program and
      # the adoption ids): an estate that still sets it is told what to set.
      resources = lib.mapAttrs (
        key: r:
        if r.options ? import then
          throw "stacks.${stack}.resources.${key}.options.import is not supported: an import left in a program destroys the adopted resource on a later `up`. Set stacks.${stack}.resources.${key}.adopt = \"<id>\" instead: `fleetkit adopt` uses it once, and it is never rendered into the program."
        else
          r
      ) config.resources;
      program = {
        name = config.project;
        runtime = "yaml";
        inherit (config) packages variables outputs;
        resources = lib.mapAttrs (
          key: r:
          prune {
            inherit (r) type;
            name = if r.name == key then null else r.name;
            properties = pt.check {
              token = r.type;
              value = r.properties;
              prefix = [
                "stacks"
                stack
                "resources"
                key
                "properties"
              ];
            };
            options = prune r.options;
          }
        ) resources;
      }
      // lib.optionalAttrs (config.description != null) { inherit (config) description; };

      # ${key...} references must name a resource or variable of this stack.
      # "$${" is a literal "${" in Pulumi YAML, not a reference.
      keys = lib.attrNames config.resources ++ lib.attrNames config.variables;
      refs = lib.unique (
        map lib.head (
          lib.filter lib.isList (
            builtins.split "\\$\\{([A-Za-z0-9_-]+)" (
              lib.replaceStrings [ "$\${" ] [ "" ] (builtins.toJSON program.resources)
            )
          )
        )
      );
      dangling = lib.filter (r: !lib.elem r keys) refs;

      # The rendered program carries no import and no adoption data: a
      # resource is type, name, properties and options, and its options have
      # no `import`. Checked on what is rendered, whatever produced it.
      leaked = lib.filter (
        key:
        let
          r = program.resources.${key};
        in
        (r.options or { }) ? import
        || removeAttrs r [
          "type"
          "name"
          "properties"
          "options"
        ] != { }
      ) (lib.attrNames program.resources);
    in
    {
      options = {
        project = mkOption {
          type = types.str;
          default = stack;
          description = "The Pulumi project name.";
        };
        estate = mkOption {
          type = types.nullOr types.str;
          default = null;
          description = "The estate whose deploy runs this stack.";
        };
        description = mkOption {
          type = types.nullOr types.str;
          default = null;
          description = "Project description.";
        };
        packages = mkOption {
          type = types.attrsOf (
            types.submodule {
              options = {
                source = mkOption {
                  type = types.str;
                  description = "Package source, e.g. terraform-provider.";
                };
                version = mkOption {
                  type = types.str;
                  description = "Its version.";
                };
                parameters = mkOption {
                  type = types.listOf types.str;
                  default = [ ];
                  description = "Parameters (the bridged provider and its version).";
                };
              };
            }
          );
          default = { };
          description = "Packages the program uses.";
        };
        resources = mkOption {
          type = types.attrsOf (types.submodule resourceModule);
          default = { };
          description = "Resources, by key.";
        };
        variables = mkOption {
          type = types.attrsOf types.raw;
          default = { };
          description = "Variables (fn::invoke and other expressions).";
        };
        outputs = mkOption {
          type = types.attrsOf types.raw;
          default = { };
          description = "Stack outputs.";
        };
        backend = mkOption {
          type = types.nullOr backendType;
          default = null;
          description = "Where the stack's state lives; null leaves it to the runner's environment.";
        };
        program = mkOption {
          type = types.raw;
          readOnly = true;
          description = "The checked program.";
        };
        file = mkOption {
          type = types.str;
          readOnly = true;
          description = "The program as Pulumi.yaml (JSON) in the store.";
        };
        secrets = mkOption {
          type = types.listOf types.str;
          readOnly = true;
          description = "Sops files the program's invokes read.";
        };
        adoptIds = mkOption {
          type = types.attrsOf types.str;
          readOnly = true;
          description = "{ <resource key> = \"<provider id>\"; } for every resource with `adopt` set: what `fleetkit adopt` imports. Never in the program.";
        };
        adoptUnresolved = mkOption {
          type = types.attrsOf types.str;
          readOnly = true;
          description = "{ <resource key> = \"<why, and what value is needed>\"; } for every resource whose `adopt` is null and whose id the model does not determine.";
        };
      };
      config = {
        program =
          if dangling != [ ] then
            throw "stacks.${stack}: references to no resource or variable: ${lib.concatStringsSep ", " dangling}"
          else if leaked != [ ] then
            throw "stacks.${stack}: the rendered program carries `import` or adoption data on: ${lib.concatStringsSep ", " leaked} (a bug in fleetkit: adoption ids are never rendered)"
          else
            program;
        adoptIds = lib.mapAttrs (_: r: r.adopt) (lib.filterAttrs (_: r: r.adopt != null) resources);
        adoptUnresolved = lib.mapAttrs (_: r: r.adoptUnresolved) (
          lib.filterAttrs (_: r: r.adopt == null && r.adoptUnresolved != null) resources
        );
        file = builtins.toFile "Pulumi.yaml" (builtins.toJSON config.program);
        secrets = lib.filter (x: x != null) (
          map (v: v."fn::invoke".arguments.sourceFile or null) (
            lib.filter (v: v ? "fn::invoke") (lib.attrValues config.variables)
          )
        );
      };
    };
in
{
  options.stacks = mkOption {
    type = types.attrsOf (types.submodule stackModule);
    default = { };
    description = "Pulumi stacks, by name.";
  };
}
