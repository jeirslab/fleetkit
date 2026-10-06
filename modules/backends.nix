# fleet.backends (state backends, id = name) and fleet.accounts
# (provider accounts, accounts.<provider>.<name>, id "<provider>/<name>").
{ lib, config, ... }:
let
  inherit (lib) types mkOption;
  h = import ./lib.nix { inherit lib; };
  ids = config.fleet.report.ids;
  # Plain str: a strMatching type error would echo a pasted secret; the
  # secretRefAssertions below validate the shape and withhold the value.
  sopsRef = types.str;
  # Backends and accounts are not owned by an estate, so only membership in
  # the declared sops keys is checked (no estate-ownership rule).
  secretRef = where: h.secretRefAssertions { inherit where; ids = ids.secretRef; };
  nullable =
    type: description:
    mkOption {
      type = types.nullOr type;
      default = null;
      inherit description;
    };

  credentialsType = types.submodule {
    options = {
      accessKeyIdRef = mkOption { type = sopsRef; };
      secretAccessKeyRef = mkOption { type = sopsRef; };
    };
  };

  bucketType =
    backend:
    types.submodule (
      { name, ... }:
      {
        options = {
          id = h.mkId (h.qualify backend name);
          name = mkOption {
            type = types.str;
            description = "Bucket name (empty = not known yet).";
          };
          keyPrefix = mkOption { type = types.str; };
        };
      }
    );

  backendType = types.submodule (
    { name, ... }:
    {
      options = {
        id = h.mkId name;
        type = mkOption {
          type = types.enum [
            "pg"
            "s3"
          ];
        };
        # pg
        schemaPrefix = nullable types.str "pg: state schema prefix.";
        connRef = nullable sopsRef "pg: sops ref of the connection string.";
        localStacks = nullable (types.listOf types.str) "pg: stacks using the local state.";
        # s3
        provider = nullable (types.enum [
          "garage"
          "linode"
        ]) "s3: provider.";
        # garage
        host = nullable types.str "garage: guest id.";
        credsRef = nullable sopsRef "garage: sops ref of the credentials.";
        # linode
        endpoint = nullable types.str "linode: endpoint URL.";
        region = nullable types.str "linode: region.";
        credentials = nullable credentialsType "linode: access keys.";
        buckets = mkOption {
          type = types.attrsOf (bucketType name);
          default = { };
          description = "linode: buckets.";
        };
      };
    }
  );

  # required / forbidden field names per variant
  variantOf = b: if b.type == "pg" then "pg" else if b.provider == null then "s3" else "s3-${b.provider}";
  fieldsOf = {
    pg = [
      "schemaPrefix"
      "connRef"
      "localStacks"
    ];
    s3-garage = [
      "host"
      "credsRef"
    ];
    s3-linode = [
      "endpoint"
      "region"
      "credentials"
    ];
  };
  allFields = lib.unique (lib.concatLists (lib.attrValues fieldsOf)) ++ [ "buckets" ];
  isSet = b: f: if f == "buckets" then b.buckets != { } else b.${f} != null;

  backendAssertions =
    bname: b:
    let
      where = "fleet.backends.${bname}";
      variant = variantOf b;
      required = fieldsOf.${variant} or [ ];
      allowed = required ++ lib.optional (variant == "s3-linode") "buckets" ++ lib.optional (lib.hasPrefix "s3" variant) "provider";
      missing = lib.filter (f: !(isSet b f)) required;
      foreign = lib.filter (f: isSet b f && !(lib.elem f allowed)) allFields;
    in
    [
      {
        assertion = b.type != "s3" || b.provider != null;
        message = "${where}: type \"s3\" requires provider (garage or linode)";
      }
      {
        assertion = b.type != "pg" || b.provider == null;
        message = "${where}: type \"pg\" must not set provider";
      }
      {
        assertion = missing == [ ];
        message = "${where}: ${variant} backend is missing required field(s): ${lib.concatStringsSep ", " missing}";
      }
      {
        assertion = foreign == [ ];
        message = "${where}: ${variant} backend must not set field(s): ${lib.concatStringsSep ", " foreign}";
      }
    ]
    ++ h.optionalRefAssertions {
      where = "${where}.host";
      kind = "guest";
      ids = ids.guest;
    } b.host
    ++ lib.concatLists (
      lib.mapAttrsToList (f: v: secretRef "${where}.${f}" v) (
        lib.filterAttrs (_: v: v != null) {
          inherit (b) connRef credsRef;
          "credentials.accessKeyIdRef" = if b.credentials == null then null else b.credentials.accessKeyIdRef;
          "credentials.secretAccessKeyRef" = if b.credentials == null then null else b.credentials.secretAccessKeyRef;
        }
      )
    );

  # ---- accounts
  knownProviders = [ "cloudflare" ];

  accountType =
    provider:
    types.submodule (
      { name, ... }:
      {
        options = {
          id = h.mkId (h.qualify provider name);
          tokenRef = mkOption { type = sopsRef; };
          usedBy = mkOption {
            type = types.listOf types.str;
            default = [ ];
            description = "Estate ids using the account.";
          };
        };
      }
    );

  # accounts.<provider> is a container of typed accounts, not a loose block.
  providerAccountsType = types.submodule (
    { name, ... }:
    {
      freeformType = types.lazyAttrsOf (accountType name);
    }
  );

  accountAssertions =
    provider: accounts:
    [
      {
        assertion = lib.elem provider knownProviders;
        message = "fleet.accounts.${provider}: unknown provider (known: ${lib.concatStringsSep ", " knownProviders})";
      }
    ]
    ++ lib.concatLists (
      lib.mapAttrsToList (
        aname: a:
        h.refAssertions {
          where = "fleet.accounts.${provider}.${aname}.usedBy";
          kind = "estate";
          ids = ids.estate;
        } a.usedBy
        ++ secretRef "fleet.accounts.${provider}.${aname}.tokenRef" a.tokenRef
      ) accounts
    );
in
{
  options.fleet = {
    backends = mkOption {
      type = types.attrsOf backendType;
      default = { };
    };
    accounts = mkOption {
      type = types.attrsOf providerAccountsType;
      default = { };
    };
  };

  config.assertions =
    lib.concatLists (lib.mapAttrsToList backendAssertions config.fleet.backends)
    ++ lib.concatLists (lib.mapAttrsToList accountAssertions config.fleet.accounts);
}
