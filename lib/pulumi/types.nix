# Nix types for Pulumi resource properties, built lazily from the pinned Pulumi
# schemas (providers/pulumi/schemas). Evaluation only.
#
#   import ./types.nix { lib; } -> { check; known; }
#
#   check { token; value; prefix; } -> value, type-checked and with unset
#     (null) fields removed; a property the schema does not have, a value of the
#     wrong type or shape, or a missing required input fails evaluation with an
#     error at `prefix` (e.g. stacks.homelab-guests.resources.box.properties).
#   known token -> whether a pinned schema describes the token.
#
# A token's types are built only when a resource of that token is checked, so
# the size of a provider's schema costs nothing until it is used (about 0.1 s
# per resource type). Every property also accepts a "${...}" reference string:
# references are checked by the program, not typed here. Tokens of packages
# with no pinned schema are not checked.
{ lib }:
let
  inherit (lib) types mkOption;

  dir = ../../providers/pulumi;
  # package name -> schema file, from the small name maps (each names its
  # package; the schema has the same base name).
  schemaFiles = lib.listToAttrs (
    map (
      f:
      let
        m = lib.importJSON (dir + "/names/${f}");
      in
      lib.nameValuePair m.package (dir + "/schemas/${lib.removeSuffix ".json" f}.pulumi.json")
    ) (lib.attrNames (lib.filterAttrs (n: t: t == "regular" && lib.hasSuffix ".json" n) (builtins.readDir (dir + "/names"))))
  );
  schemas = lib.mapAttrs (_: lib.importJSON) schemaFiles;

  packageOf =
    token:
    if lib.hasPrefix "pulumi:providers:" token then
      lib.removePrefix "pulumi:providers:" token
    else
      lib.head (lib.splitString ":" token);

  # The input properties (and the required ones) of a token, or null.
  inputsOf =
    token:
    let
      s = schemas.${packageOf token} or null;
    in
    if s == null then
      null
    else if lib.hasPrefix "pulumi:providers:" token then
      {
        props = (s.config.variables or { }) // (s.provider.inputProperties or { });
        required = s.provider.requiredInputs or [ ];
        inherit s;
      }
    else if s.resources ? ${token} then
      {
        props = s.resources.${token}.inputProperties or { };
        required = s.resources.${token}.requiredInputs or [ ];
        inherit s;
      }
    else
      null;

  ref = types.strMatching "\\$\\{.+}.*|.*\\$\\{.+}";
  orRef = t: types.either t ref;

  typeOf =
    s: p:
    let
      t = p.type or null;
    in
    if p ? "$ref" then
      let
        r = lib.removePrefix "#/types/" p."$ref";
      in
      if lib.hasPrefix "pulumi.json#/" p."$ref" then
        types.raw
      else
        types.submodule { options = optionsOf s (s.types.${r}.properties or { }); }
    else if t == "array" then
      types.listOf (orRef (typeOf s (p.items or { })))
    else if t == "object" then
      types.attrsOf (orRef (typeOf s (p.additionalProperties or { type = "string"; })))
    else if t == "boolean" then
      types.bool
    else if t == "integer" then
      types.int
    else if t == "number" then
      types.either types.int types.float
    else if t == "string" then
      types.str
    else
      types.raw;

  optionsOf =
    s: props:
    lib.mapAttrs (
      _: p:
      mkOption {
        type = types.nullOr (orRef (typeOf s p));
        default = null;
        description = p.description or "";
      }
    ) props;

  prune =
    v:
    if lib.isAttrs v then
      lib.mapAttrs (_: prune) (lib.filterAttrs (_: x: x != null) v)
    else if lib.isList v then
      map prune v
    else
      v;
in
{
  known = token: inputsOf token != null;

  check =
    {
      token,
      value,
      prefix,
    }:
    let
      i = inputsOf token;
      where = lib.concatStringsSep "." prefix;
      checked =
        (lib.evalModules {
          inherit prefix;
          modules = [
            { options = optionsOf i.s i.props; }
            {
              _file = where;
              config = value;
            }
          ];
        }).config;
      missing = lib.filter (n: (value.${n} or null) == null) i.required;
      result = prune (lib.getAttrs (lib.attrNames i.props) checked);
    in
    if i == null then
      value
    # An unknown or mistyped property is the likelier mistake (a misspelt
    # required one is also "missing"), so its error comes first.
    else
      builtins.deepSeq result (
        if missing != [ ] then throw "${where}: ${token} requires ${lib.concatStringsSep ", " missing}" else result
      );
}
