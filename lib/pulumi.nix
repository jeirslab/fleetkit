# toPulumi: translate a rendered main.tf.json attrset (lib.mkTerraform,
# lib.mkGithubTerraform) into a Pulumi YAML program (builtins.toJSON it into
# Pulumi.yaml; YAML reads JSON). Evaluation only; nothing here runs pulumi.
#
#   toPulumi { lib; tf; project; description ? null; adopt ? { }; }
#     -> { name; runtime = "yaml"; packages; resources; variables; outputs; }
#
# Experimental (branch experimental-pulumi): the Terraform render stays the one
# source of truth and this is a second spelling of it, so both engines plan the
# same model.
#
# Each Terraform provider is used through Pulumi's terraform-provider bridge at
# the exact source and version the render pins (packages.<pkg>.parameters), not
# through a separately versioned Pulumi package, so the provider code that runs
# is the one the guest model is checked against. The bridge renames fields
# (camelCase, plural lists, one-item lists as objects, id as <resource>Id);
# providers/pulumi/names/<provider>-<version>.json (tests/gen_pulumi_names.py)
# records every rename, and a field it does not know fails evaluation.
#
#   resource.<type>.<name>     resources.<name>; the logical name stays the
#                              Terraform name. When two types share a name the
#                              key is <name>_<type without provider prefix>
#                              and `name:` keeps the logical name.
#   provider.<p>               resources.provider-<p> (pulumi:providers:<pkg>),
#                              set as options.provider on that provider's
#                              resources
#   data.<type>.<name>         variables.<type>_<name> (fn::invoke)
#   lifecycle.prevent_destroy  options.protect
#   lifecycle.ignore_changes   options.ignoreChanges (renamed paths)
#   depends_on                 options.dependsOn
#   ${type.name.attr}          ${key.attr}, renamed; ${data.type.name.attr}
#                              likewise through the variable
#   locals                     outputs (informational, as in Terraform)
#
# `adopt` maps a Terraform resource type to a function (name -> args -> the
# provider's import id). Each resource of such a type gets options.import, so
# the first `pulumi up` adopts what is already deployed instead of creating it;
# Pulumi refuses an adoption whose inputs differ from the live resource, which
# makes that first run a check of the model against the live estate.
{
  lib,
  tf,
  project,
  description ? null,
  adopt ? { },
}:
let
  where = "toPulumi ${project}";

  maps =
    let
      dir = ../providers/pulumi/names;
    in
    map (f: lib.importJSON (dir + "/${f}")) (
      lib.attrNames (lib.filterAttrs (n: t: t == "regular" && lib.hasSuffix ".json" n) (builtins.readDir dir))
    );
  bySource = lib.listToAttrs (map (m: lib.nameValuePair m.source m) maps);

  # Terraform local provider name -> its name map, at the pinned version.
  required = tf.terraform.required_providers or { };
  providers = lib.mapAttrs (
    _: rp:
    let
      m = bySource.${rp.source} or (throw "${where}: provider ${rp.source} has no name map under providers/pulumi/names");
    in
    if m.version != rp.version then
      throw "${where}: provider ${rp.source} is pinned at ${rp.version} but its name map is for ${m.version}"
    else
      m
  ) required;

  # A Terraform type's provider is its prefix up to the first underscore.
  localOf = type: lib.head (lib.splitString "_" type);
  mapOf =
    type:
    providers.${localOf type}
      or (throw "${where}: ${type} belongs to provider \"${localOf type}\", which is not in terraform.required_providers");
  resourceMap =
    type: (mapOf type).resources.${type} or (throw "${where}: resource type ${type} is not in ${(mapOf type).source}");
  dataMap =
    type:
    (mapOf type).dataSources.${type} or (throw "${where}: data source ${type} is not in ${(mapOf type).source}");
  shortOf = type: lib.removePrefix "${localOf type}_" type;

  resources = tf.resource or { };
  datas = tf.data or { };
  tfProviders = tf.provider or { };

  # ── keys ───────────────────────────────────────────────────────────────
  nameCount = lib.foldl' (acc: n: acc // { ${n} = (acc.${n} or 0) + 1; }) { } (
    lib.concatLists (lib.mapAttrsToList (_: lib.attrNames) resources)
  );
  resKey = type: name: if nameCount.${name} > 1 then "${name}_${shortOf type}" else name;
  dataKey = type: name: "${type}_${name}";
  providerKey = local: "provider-${local}";

  allKeys =
    lib.concatLists (lib.mapAttrsToList (t: rs: map (resKey t) (lib.attrNames rs)) resources)
    ++ lib.concatLists (lib.mapAttrsToList (t: ds: map (dataKey t) (lib.attrNames ds)) datas)
    ++ map providerKey (lib.attrNames tfProviders);
  dupKeys = lib.attrNames (
    lib.filterAttrs (_: c: c > 1) (lib.foldl' (acc: k: acc // { ${k} = (acc.${k} or 0) + 1; }) { } allKeys)
  );

  # ── expressions ────────────────────────────────────────────────────────
  # The part of a reference after the object: .attr, ["key"] or [0], renamed
  # along the field map. An index into a field Pulumi holds as an object (a
  # one-item list in Terraform) is dropped.
  convRest =
    fields: rest:
    let
      toks = lib.filter (t: lib.isList t) (builtins.split "(\\.[A-Za-z_][A-Za-z0-9_-]*|\\[[^]]*])" rest);
      step =
        acc: tok:
        let
          t = lib.head tok;
        in
        if lib.hasPrefix "." t then
          let
            n = lib.removePrefix "." t;
            e =
              if acc.fields == null then
                {
                  inherit n;
                  f = null;
                }
              else
                acc.fields.${n} or (throw "${where}: reference ${rest}: no field ${n}");
          in
          {
            out = acc.out + ".${e.n}";
            fields = e.f or null;
            entry = e;
          }
        else if acc.entry != null && !(acc.entry.arr or false) && !(acc.entry.map or false) then
          # [0] into a one-item list Pulumi flattened: no index there.
          acc // { entry = null; }
        else
          acc
          // {
            out = acc.out + t;
            entry = null;
          };
    in
    (lib.foldl' step {
      out = "";
      inherit fields;
      entry = null;
    } toks).out;

  convRef =
    inner:
    let
      dm = builtins.match "data\\.([A-Za-z0-9_-]+)\\.([A-Za-z0-9_-]+)(.*)" inner;
      rm = builtins.match "([A-Za-z0-9_-]+)\\.([A-Za-z0-9_-]+)(.*)" inner;
    in
    if dm != null then
      let
        type = lib.elemAt dm 0;
        name = lib.elemAt dm 1;
      in
      if !(datas ? ${type}.${name}) then
        throw "${where}: reference \${${inner}} names a data source that is not rendered"
      else
        "\${${dataKey type name}${convRest (dataMap type).f (lib.elemAt dm 2)}}"
    else if rm != null && resources ? ${lib.elemAt rm 0}.${lib.elemAt rm 1} then
      let
        type = lib.elemAt rm 0;
        key = resKey type (lib.elemAt rm 1);
        rest = lib.elemAt rm 2;
      in
      # A resource's Terraform id is its Pulumi id. The bridge also exposes an
      # SDKv2 id as an input (<resource>Id), but only .id is always the same
      # value.
      if rest == ".id" then
        "\${${key}.id}"
      else
        "\${${key}${convRest (resourceMap type).f rest}}"
    else
      throw "${where}: cannot translate \${${inner}}; only references to rendered resources and data sources are supported";

  # $${ is a literal "${" in both languages; it passes through unchanged.
  convStr =
    s:
    lib.concatMapStrings (
      p:
      if lib.isString p then
        p
      else if lib.head p == "$\${" then
        "$\${"
      else
        convRef (lib.removeSuffix "}" (lib.removePrefix "\${" (lib.head p)))
    ) (builtins.split "(\\$\\$\\{|\\$\\{[^}]*})" s);

  convPlain =
    v:
    if lib.isString v then
      convStr v
    else if lib.isList v then
      map convPlain v
    else if lib.isAttrs v then
      lib.mapAttrs (_: convPlain) v
    else
      v;

  # ── arguments ──────────────────────────────────────────────────────────
  dropNulls = lib.filterAttrs (_: v: v != null);

  convFields =
    what: fields: args:
    dropNulls (
      lib.mapAttrs' (
        k: v:
        let
          e = fields.${k} or (throw "${where}: ${what} has no field ${k} in the pinned provider");
        in
        lib.nameValuePair e.n (convField "${what}.${k}" e v)
      ) args
    );

  convField =
    what: e: v:
    let
      arr = e.arr or false;
      shaped =
        if arr && lib.isAttrs v then
          [ v ]
        else if !arr && lib.isList v && e ? f then
          (
            if v == [ ] then
              null
            else if lib.length v == 1 then
              lib.head v
            else
              throw "${where}: ${what} takes one item in Pulumi, the render has ${toString (lib.length v)}"
          )
        else
          v;
      inner = x: if e ? f && lib.isAttrs x then convFields what e.f x else convPlain x;
    in
    if shaped == null then
      null
    else if e.map or false then
      lib.mapAttrs (_: inner) shaped
    else if lib.isList shaped then
      map inner shaped
    else
      inner shaped;

  # ignore_changes paths (a.b, a[0].b) renamed along the field map.
  convPath =
    fields: path:
    let
      r = convRest fields ".${path}";
    in
    lib.removePrefix "." r;

  # ── program ────────────────────────────────────────────────────────────
  meta = [
    "lifecycle"
    "depends_on"
    "provider"
  ];

  mkResource =
    type: name: body:
    let
      m = resourceMap type;
      local = localOf type;
      lc = body.lifecycle or { };
      unsupported = lib.attrNames (
        removeAttrs lc [
          "prevent_destroy"
          "ignore_changes"
        ]
      );
      extra = lib.attrNames (
        lib.filterAttrs (k: _: lib.elem k [
          "count"
          "for_each"
          "provisioner"
          "connection"
        ]) body
      );
      options = dropNulls {
        provider = if tfProviders ? ${local} then "\${${providerKey local}}" else null;
        protect = if lc.prevent_destroy or false then true else null;
        ignoreChanges = if lc ? ignore_changes then map (convPath m.f) lc.ignore_changes else null;
        dependsOn =
          if body ? depends_on then
            map (
              d:
              let
                dm = builtins.match "([A-Za-z0-9_-]+)\\.([A-Za-z0-9_-]+)" d;
              in
              if dm == null then
                throw "${where}: ${type}.${name}: depends_on ${d} is not <type>.<name>"
              else
                "\${${resKey (lib.elemAt dm 0) (lib.elemAt dm 1)}}"
            ) body.depends_on
          else
            null;
        import = if adopt ? ${type} then adopt.${type} name body else null;
      };
    in
    if unsupported != [ ] then
      throw "${where}: ${type}.${name}: lifecycle ${lib.concatStringsSep ", " unsupported} has no Pulumi translation here"
    else if extra != [ ] then
      throw "${where}: ${type}.${name}: ${lib.concatStringsSep ", " extra} has no Pulumi translation here"
    else
      lib.nameValuePair (resKey type name) (
        {
          type = m.token;
          properties = convFields "${type}.${name}" m.f (removeAttrs body meta);
        }
        // lib.optionalAttrs (resKey type name != name) { inherit name; }
        // lib.optionalAttrs (options != { }) { inherit options; }
      );

  mkProvider =
    local: args:
    let
      m = providers.${local} or (throw "${where}: provider.${local} is not in terraform.required_providers");
    in
    lib.nameValuePair (providerKey local) {
      type = "pulumi:providers:${m.package}";
      properties = convFields "provider.${local}" m.provider args;
    };

  mkData =
    type: name: args:
    let
      m = dataMap type;
    in
    lib.nameValuePair (dataKey type name) {
      "fn::invoke" = {
        function = m.token;
        arguments = convFields "data.${type}.${name}" m.f args;
      }
      // lib.optionalAttrs (tfProviders ? ${localOf type}) {
        options.provider = "\${${providerKey (localOf type)}}";
      };
    };

  program = {
    name = project;
    runtime = "yaml";
    packages = lib.mapAttrs' (
      _: m:
      lib.nameValuePair m.package {
        source = "terraform-provider";
        version = m.bridge;
        parameters = [
          m.source
          m.version
        ];
      }
    ) providers;
    resources =
      lib.listToAttrs (lib.mapAttrsToList mkProvider tfProviders)
      // lib.listToAttrs (
        lib.concatLists (lib.mapAttrsToList (type: rs: lib.mapAttrsToList (mkResource type) rs) resources)
      );
    variables = lib.listToAttrs (
      lib.concatLists (lib.mapAttrsToList (type: ds: lib.mapAttrsToList (mkData type) ds) datas)
    );
    outputs = tf.locals or { };
  }
  // lib.optionalAttrs (description != null) { inherit description; };
in
if dupKeys != [ ] then
  throw "${where}: program keys collide: ${lib.concatStringsSep ", " dupKeys}"
else
  program
