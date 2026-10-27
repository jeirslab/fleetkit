# fleet.estates.<estate>.secrets: the sops file registry. Each file declares
# the key NAMES it holds; `ref.<key>` is derived as "sops:<estate>/<file>#<key>"
# for every declared key and for nothing else, so `files.<f>.ref."<undeclared>"` is an
# evaluation error ("attribute '<undeclared>' missing"). Values are never
# read.
#
# What is new for a secrets data repository is marked "ADR-0009" below
# (ADR-0017 keeps it in fleetkit): per-key Infisical and Bitwarden placements,
# an estate's verbatim .sops.yaml layout, the multi-estate .sops.yaml
# (fleet.sopsConfig), fleet.secretHosts and the fleet.report.secrets.* views.
# Part of every mkFleet (modules/options.nix imports it) and, with
# lib/secrets/standalone.nix, of lib.secrets.evalRegistry.
{ lib, config, ... }:
let
  inherit (lib) types mkOption;
  h = import ./lib.nix { inherit lib; };
  placement = import ./secrets-placements.nix { inherit lib; };

  # Age recipient anchors are derived per estate from `secrets.anchors`
  #. Closed per estate so a
  # misspelled recipient fails evaluation.
  topConfig = config;

  estateGuestIds = estate: h.idsOf (topConfig.fleet.guests.${estate} or { });

  # Public age recipient: age1 + 58 bech32 characters.
  ageRecipient = types.strMatching "age1[02-9ac-hj-np-z]{58}";

  # Operator anchors of an estate (read lazily by the file defaults below).
  estateOperatorNames = estate: lib.attrNames topConfig.fleet.estates.${estate}.secrets.operators;

  anchorsType =
    estate:
    types.submodule (
      { config, ... }:
      let
        guestIds = estateGuestIds estate;
        hostIds = config.hosts ++ lib.optionals config.includeEstateGuests guestIds;
        render =
          id:
          lib.replaceStrings
            [ "{name}" ]
            [
              (config.aliases.${id} or (lib.replaceStrings [ "-" ] [ "_" ] (lib.last (lib.splitString "/" id))))
            ]
            config.pattern;
      in
      {
        options = {
          pattern = mkOption {
            type = types.str;
            default = "{name}";
            description = "Anchor name template; must contain {name} exactly once.";
          };
          includeEstateGuests = mkOption {
            type = types.bool;
            default = false;
            description = "Give every guest of the estate an anchor.";
          };
          hosts = mkOption {
            type = types.listOf types.str;
            default = [ ];
            description = "Guest or node ids that get an anchor (also outside the estate).";
          };
          aliases = mkOption {
            type = types.attrsOf types.str;
            default = { };
            description = "Host id -> name used for {name} where the legacy anchor differs.";
          };
          extra = mkOption {
            type = types.listOf types.str;
            default = [ ];
            description = "Anchors that are not hosts (operator keys, stale anchors).";
          };
          all = mkOption {
            type = types.listOf types.str;
            readOnly = true;
            default = config.extra ++ map render hostIds;
            description = "Derived anchor set.";
          };
          rendered = mkOption {
            type = types.listOf types.str;
            readOnly = true;
            internal = true;
            default = hostIds;
            description = "Host ids that receive an anchor.";
          };
        };
      }
    );

  fileType =
    estate:
    types.submodule (
      { name, config, ... }:
      {
        options = {
          id = h.mkId (h.qualify estate name);
          path = mkOption {
            type = types.str;
            description = "Path of the sops file, relative to the estate's repo.";
          };
          keys = mkOption {
            type = types.listOf types.str;
            description = "Key names declared in the file (names only, never values).";
          };
          readers = mkOption {
            type = types.listOf types.str;
            default = [ ];
            description = "Guest or node ids that decrypt this file (their hostKeys.age or hostKeys.ed25519 becomes a recipient).";
          };
          operators = mkOption {
            type = types.listOf types.str;
            default = estateOperatorNames estate;
            defaultText = lib.literalExpression "attrNames fleet.estates.<estate>.secrets.operators";
            description = "Operator anchor names (secrets.operators) that can decrypt this file. Default: all of the estate's operators.";
          };
          sharedWith = mkOption {
            type = types.listOf types.str;
            default = [ ];
            description = "Estates whose repositories may take a key of this file as an Actions secret (repos.<estate>.<key>.actions.secrets.<NAME>.sourceRef). The owning estate's explicit grant; without it a repository can only use its own estate's files.";
          };
          ref = mkOption {
            type = types.lazyAttrsOf types.str;
            readOnly = true;
            default = lib.genAttrs config.keys (k: "sops:${estate}/${name}#${k}");
            description = "ref.<key> = \"sops:<estate>/<file>#<key>\" for every declared key; any other key is an evaluation error.";
          };
          # ADR-0009
          placements = mkOption {
            type = types.attrsOf placement.type;
            default = { };
            description = "Declared key -> where its value is mirrored: infisical (the fields of nixfisical's mkInfisical) and/or bitwarden (the fields of nixwarden's mkBitwarden). Names and places only, never values.";
          };
        };
      }
    );

  secretsType =
    estate:
    types.submodule {
      options = {
        backend = mkOption {
          type = types.enum [
            "sops"
            "sops+infisical"
          ];
        };
        anchors = mkOption {
          type = anchorsType estate;
          default = { };
          description = "Derived age recipient anchors of the estate's .sops.yaml.";
        };
        recipients = mkOption {
          type = types.listOf types.str;
          default = [ ];
          description = "Recipient names (age key aliases).";
        };
        operators = mkOption {
          type = types.attrsOf (
            types.submodule {
              options.age = mkOption {
                type = ageRecipient;
                description = "Public age recipient (age1...) of an operator anchor that is not a host.";
              };
            }
          );
          default = { };
          description = "Operator anchors that are not hosts: name -> public age recipient.";
        };
        files = mkOption {
          type = types.lazyAttrsOf (fileType estate);
          default = { };
        };
        # ADR-0009
        layout = mkOption {
          type = layoutType;
          default = { };
          description = "How this estate is written into a multi-estate .sops.yaml (fleet.report.secrets.sopsConfig): an anchor prefix, comments, and anchors and rules kept verbatim, in their order (an estate whose .sops.yaml was written by hand).";
        };
      };
    };

  # ADR-0009: an estate's part of a multi-estate .sops.yaml.
  layoutType = types.submodule {
    options = {
      prefix = mkOption {
        type = types.str;
        default = "";
        description = "Prefix of every anchor name of this estate in the rendered .sops.yaml, so two estates' anchors cannot clash.";
      };
      root = mkOption {
        type = types.str;
        default = "";
        description = "Directory the verbatim rules are re-rooted under: an anchored path_regex (^x) becomes ^<root>/x, an unanchored one ^<root>/.*x. Empty: kept as written. Does not apply to the rules derived from files (their path is already relative to the repository).";
      };
      keysComment = mkOption {
        type = types.nullOr types.str;
        default = null;
        description = "Comment written before this estate's anchors (one '  # ' line per line).";
      };
      rulesComment = mkOption {
        type = types.nullOr types.str;
        default = null;
        description = "Comment written before this estate's rules (one '  # ' line per line).";
      };
      keys = mkOption {
        type = types.listOf (
          types.submodule {
            options = {
              name = mkOption { type = types.str; };
              age = mkOption { type = ageRecipient; };
            };
          }
        );
        default = [ ];
        description = "Verbatim anchors, written in this order after the derived ones.";
      };
      rules = mkOption {
        type = types.listOf (
          types.submodule {
            options = {
              pathRegex = mkOption { type = types.str; };
              anchors = mkOption { type = types.listOf types.str; };
            };
          }
        );
        default = [ ];
        description = "Verbatim creation rules (path_regex as written, re-rooted under root), written in this order after the derived ones. Anchors are names of this estate (verbatim keys or derived anchors), without the prefix.";
      };
    };
  };

  dups = keys: lib.filter (k: lib.count (x: x == k) keys > 1) (lib.unique keys);

  # Name of a reader's anchor in fleet.report.sops: its alias, else
  # host_<last id segment>. Two ids can share a last segment, and an operator
  # can carry such a name; both are refused by an assertion below, because the
  # report keys its anchors by this name.
  sopsAnchorName =
    a: id:
    a.aliases.${id} or "host_${lib.replaceStrings [ "-" ] [ "_" ] (lib.last (lib.splitString "/" id))}";
  sopsReaderIds = e: lib.unique (lib.concatMap (f: f.readers) (lib.attrValues e.secrets.files));

  fileAssertions =
    ename: fname: f:
    let
      w = "fleet.estates.${ename}.secrets.files.${fname}.keys";
    in
    [
      {
        assertion = f.keys != [ ];
        message = "${w}: must declare at least one key";
      }
      {
        assertion = lib.all (k: k != "") f.keys;
        message = "${w}: key names must be non-empty";
      }
      {
        # Shape: path-like segments of [A-Za-z0-9_.-] joined by single slashes.
        assertion = lib.all (
          k: k == "" || builtins.match "[A-Za-z0-9_.-]+(/[A-Za-z0-9_.-]+)*" k != null
        ) f.keys;
        message = "${w}: key names must be slash-separated segments of [A-Za-z0-9_.-]; bad: ${
          lib.concatStringsSep ", " (
            lib.filter (k: k != "" && builtins.match "[A-Za-z0-9_.-]+(/[A-Za-z0-9_.-]+)*" k == null) f.keys
          )
        }";
      }
      {
        assertion = dups f.keys == [ ];
        message = "${w}: duplicate keys: ${lib.concatStringsSep ", " (dups f.keys)}";
      }
    ];

  # ADR-0009 ----------------------------------------------------------------
  estatesCfg = config.fleet.estates;
  inherit (lib) concatStringsSep;

  # The type and key of an ssh host key, without a trailing comment.
  bareKey = k: if k == null then null else concatStringsSep " " (lib.take 2 (lib.splitString " " k));

  # Every placement of the registry, flat.
  placementList = lib.concatLists (
    lib.mapAttrsToList (
      ename: e:
      lib.concatLists (
        lib.mapAttrsToList (
          fname: f:
          lib.mapAttrsToList (key: p: {
            inherit
              ename
              fname
              key
              p
              f
              ;
          }) f.placements
        ) e.secrets.files
      )
    ) estatesCfg
  );
  placedAt = x: "${x.ename}/${x.fname}#${x.key}";
  infisicalName =
    x: if x.p.infisical.name != null then x.p.infisical.name else placement.lastSeg x.key;
  bitwardenName =
    x: if x.p.bitwarden.name != null then x.p.bitwarden.name else placement.lastSeg x.key;
  infisicalPlaced = lib.filter (x: x.p.infisical != null) placementList;
  bitwardenPlaced = lib.filter (x: x.p.bitwarden != null) placementList;
  clashes = coord: xs: lib.filterAttrs (_: ys: lib.length ys > 1) (lib.groupBy coord xs);
  clashText =
    cs:
    concatStringsSep "; " (
      lib.mapAttrsToList (c: ys: "${c} <- ${concatStringsSep ", " (map placedAt ys)}") cs
    );
  infisicalClashes = clashes (
    x: placement.infisicalCoordinate x.p.infisical (infisicalName x)
  ) infisicalPlaced;
  bitwardenClashes = clashes (
    x: placement.bitwardenCoordinate x.p.bitwarden (bitwardenName x)
  ) bitwardenPlaced;

  placementAssertions =
    ename: fname: f:
    let
      w = "fleet.estates.${ename}.secrets.files.${fname}.placements";
      files = estatesCfg.${ename}.secrets.files;
    in
    lib.concatLists (
      lib.mapAttrsToList (
        key: p:
        let
          wk = "${w}.\"${key}\"";
          i = p.infisical;
          b = p.bitwarden;
          field = if b.field != null then b.field else placement.defaultField b.type;
        in
        [
          {
            assertion = lib.elem key f.keys;
            message = "${wk}: \"${key}\" is not a declared key of the file (keys: ${concatStringsSep ", " f.keys})";
          }
        ]
        ++ lib.optionals (i != null) [
          {
            assertion = i.project != "";
            message = "${wk}.infisical.project: must be non-empty";
          }
          {
            assertion = lib.hasPrefix "/" i.folder;
            message = "${wk}.infisical.folder: must be absolute (start with /), got \"${i.folder}\"";
          }
          {
            assertion = builtins.match "[a-z0-9-]+" i.environment != null;
            message = "${wk}.infisical.environment: must match [a-z0-9-]+, got \"${i.environment}\"";
          }
        ]
        ++ lib.optionals (b != null) (
          [
            {
              assertion = b.org != "";
              message = "${wk}.bitwarden.org: must be non-empty";
            }
            {
              assertion = b.collection != "";
              message = "${wk}.bitwarden.collection: must be non-empty";
            }
            {
              assertion = lib.elem field (placement.fieldsFor b.type);
              message = "${wk}.bitwarden.field: \"${field}\" is not a slot of a ${b.type} item (${concatStringsSep ", " (placement.fieldsFor b.type)})";
            }
            {
              assertion = b.type == "login" || (b.username == null && b.totp == null && b.uris == [ ]);
              message = "${wk}.bitwarden: username, totp and uris only apply to login items";
            }
            {
              assertion =
                !(
                  (field == "username" && b.username != null)
                  || (field == "totp" && b.totp != null)
                  || (field == "notes" && b.notes != null)
                );
              message = "${wk}.bitwarden: the key's value and a declared ${field} both target the same slot";
            }
            {
              assertion = !(b.fields ? nixwarden);
              message = "${wk}.bitwarden.fields: \"nixwarden\" is reserved for nixwarden's managed marker";
            }
          ]
          ++ map (
            s:
            let
              tf = if s.value.sopsFile == null then fname else s.value.sopsFile;
            in
            {
              assertion = files ? ${tf} && lib.elem s.value.sopsKey files.${tf}.keys;
              message = "${wk}.bitwarden.${s.name}: \"${s.value.sopsKey}\" is not a declared key of fleet.estates.${ename}.secrets.files.${tf}";
            }
          ) (placement.refSlots b)
        )
      ) f.placements
    );

  # The anchor names an estate contributes to the multi-estate .sops.yaml,
  # without its prefix: the derived ones, then the verbatim ones.
  layoutNames =
    ename:
    lib.attrNames (config.fleet.report.sops.${ename}.anchors or { })
    ++ map (k: k.name) estatesCfg.${ename}.secrets.layout.keys;

  sopsConfigAssertions =
    let
      order = config.fleet.sopsConfig.estates;
      known = lib.filter (n: estatesCfg ? ${n}) order;
      prefixed = lib.concatMap (
        n: map (x: estatesCfg.${n}.secrets.layout.prefix + x) (layoutNames n)
      ) known;
    in
    map (n: {
      assertion = estatesCfg ? ${n};
      message = "fleet.sopsConfig.estates: \"${n}\" is not a declared estate";
    }) order
    ++ [
      {
        assertion = dups order == [ ];
        message = "fleet.sopsConfig.estates: listed twice: ${concatStringsSep ", " (dups order)}";
      }
      {
        assertion = dups prefixed == [ ];
        message = "fleet.sopsConfig: anchor names clash in the rendered .sops.yaml: ${concatStringsSep ", " (dups prefixed)}; give an estate a secrets.layout.prefix";
      }
    ]
    ++ lib.concatMap (
      n:
      let
        l = estatesCfg.${n}.secrets.layout;
        names = layoutNames n;
      in
      lib.concatLists (
        lib.imap0 (
          idx: r:
          [
            {
              assertion = r.anchors != [ ];
              message = "fleet.estates.${n}.secrets.layout.rules.${toString idx} (${r.pathRegex}): no anchors";
            }
          ]
          ++ map (a: {
            assertion = lib.elem a names;
            message = "fleet.estates.${n}.secrets.layout.rules.${toString idx} (${r.pathRegex}): anchor \"${a}\" is not one of the estate's (${concatStringsSep ", " names})";
          }) r.anchors
        ) l.rules
      )
    ) known;

  hostIdAssertions = map (id: {
    assertion = builtins.match "[^/]+/.+" id != null;
    message = "fleet.secretHosts.\"${id}\": a host id is <estate or site>/<name>";
  }) (lib.attrNames config.fleet.secretHosts);
in
{
  options.fleet.estates = mkOption {
    type = types.attrsOf (
      types.submodule (
        { name, ... }:
        {
          options.secrets = mkOption {
            type = secretsType name;
            default = { };
          };
        }
      )
    );
  };

  config.assertions =
    # ADR-0009
    lib.concatLists (
      lib.mapAttrsToList (
        ename: e: lib.concatLists (lib.mapAttrsToList (placementAssertions ename) e.secrets.files)
      ) config.fleet.estates
    )
    ++ [
      {
        assertion = infisicalClashes == { };
        message = "fleet.estates: two keys are placed at the same Infisical destination (project/environment/folder:name): ${clashText infisicalClashes}";
      }
      {
        assertion = bitwardenClashes == { };
        message = "fleet.estates: two keys are placed into the same Bitwarden item (org/collection:name): ${clashText bitwardenClashes}";
      }
    ]
    ++ sopsConfigAssertions
    ++ hostIdAssertions
    # From fleetkit, unchanged.
    ++ lib.concatLists (
      lib.mapAttrsToList (
        ename: e: lib.concatLists (lib.mapAttrsToList (fileAssertions ename) e.secrets.files)
      ) config.fleet.estates
    )
    ++ lib.concatLists (
      lib.mapAttrsToList (
        ename: e:
        let
          a = e.secrets.anchors;
          w = "fleet.estates.${ename}.secrets.anchors";
          ids = config.fleet.report.ids;
          readerIds = sopsReaderIds e;
          readersByAnchor = lib.groupBy (sopsAnchorName a) readerIds;
          sharedAnchors = lib.filterAttrs (_: rs: lib.length rs > 1) readersByAnchor;
          operatorClashes = lib.filterAttrs (n: _: e.secrets.operators ? ${n}) readersByAnchor;
          hint = "list the reader in ${w}.hosts and give it a distinct name in ${w}.aliases";
        in
        [
          {
            assertion = lib.length (lib.splitString "{name}" a.pattern) == 2;
            message = "${w}.pattern: must contain {name} exactly once, got \"${a.pattern}\"";
          }
          {
            assertion = dups a.all == [ ];
            message = "${w}.all: duplicate anchors: ${lib.concatStringsSep ", " (dups a.all)}";
          }
        ]
        ++ h.refAssertions {
          where = "${w}.hosts";
          kind = "host";
          ids = ids.guest ++ ids.node;
        } a.hosts
        ++ map (id: {
          assertion = lib.elem id a.rendered;
          message = "${w}.aliases: \"${id}\" is not a host of this estate's anchors";
        }) (lib.attrNames a.aliases)
        ++ lib.concatLists (
          lib.mapAttrsToList (
            fname: f:
            h.refAssertions {
              where = "fleet.estates.${ename}.secrets.files.${fname}.readers";
              kind = "host";
              ids = ids.guest ++ ids.node;
            } f.readers
            ++ map (o: {
              assertion = e.secrets.operators ? ${o};
              message = "fleet.estates.${ename}.secrets.files.${fname}.operators: \"${o}\" is not declared in fleet.estates.${ename}.secrets.operators";
            }) f.operators
          ) e.secrets.files
        )
        # The report keys anchors by name: a shared name would encrypt a file to
        # the wrong recipient without any other sign.
        ++ [
          {
            assertion = sharedAnchors == { };
            message = "fleet.estates.${ename}.secrets.files: readers share a sops anchor name: ${
              lib.concatStringsSep "; " (
                lib.mapAttrsToList (n: rs: "${n} <- ${lib.concatStringsSep ", " rs}") sharedAnchors
              )
            }; ${hint}";
          }
          {
            assertion = operatorClashes == { };
            message = "fleet.estates.${ename}.secrets.files: a reader's sops anchor name is also an operator in fleet.estates.${ename}.secrets.operators: ${
              lib.concatStringsSep "; " (
                lib.mapAttrsToList (n: rs: "${n} <- ${lib.concatStringsSep ", " rs}") operatorClashes
              )
            }; ${hint}";
          }
        ]
        ++ h.refAssertions {
          where = "fleet.estates.${ename}.secrets.recipients";
          kind = "anchor";
          ids = a.all;
        } e.secrets.recipients
      ) config.fleet.estates
    );

  options.fleet = {
    # ADR-0009
    sopsConfig = {
      header = mkOption {
        type = types.nullOr types.str;
        default = null;
        description = "Comment at the top of the rendered multi-estate .sops.yaml; each line becomes '# <line>'.";
      };
      estates = mkOption {
        type = types.listOf types.str;
        default = lib.attrNames (
          lib.filterAttrs (
            _: e: e.secrets.files != { } || e.secrets.layout.keys != [ ] || e.secrets.layout.rules != [ ]
          ) config.fleet.estates
        );
        defaultText = lib.literalMD "every estate with files or a verbatim layout, by name";
        description = "Estates written into the multi-estate .sops.yaml, in this order: each one's anchors, then each one's rules.";
      };
      protect = mkOption {
        type = types.listOf types.str;
        default = [ ];
        description = "Regular expressions (searched in the repository-relative path) of files the secrets CLI never re-encrypts: rekey and host add skip them and say so. Files that must move only as bytes.";
      };
    };

    # ADR-0009
    secretHosts = mkOption {
      type = types.attrsOf (
        types.submodule {
          options = {
            kind = mkOption {
              type = types.enum [
                "guest"
                "node"
              ];
              default = "guest";
              description = "guest: <estate>/<name> under fleet.guests; node: <site>/<name> under fleet.sites.<site>.nodes.";
            };
            ed25519 = mkOption {
              type = types.nullOr (types.strMatching "ssh-ed25519 AAAA[A-Za-z0-9+/]+={0,2}( [^\n]*)?");
              default = null;
            };
            age = mkOption {
              type = types.nullOr ageRecipient;
              default = null;
            };
          };
        }
      );
      default = { };
      description = "Host keys kept by a data repository: id -> public keys. Evaluated on its own (lib.secrets.evalRegistry) these are the guests and nodes readers name; inside a fleet model the model's hostKeys are used, and fleet.report.secrets.hostDrift lists where the two disagree.";
    };

    report = {
      # ADR-0009
      # Untyped like the other derived reports: never set by config.
      secrets = mkOption {
        readOnly = true;
        description = "Derived, read-only (ADR-0009): sopsConfig (the multi-estate .sops.yaml renderer's input), files (every registry file with its keys and readers), placements.{infisical,bitwarden} (manifests in nixfisical's and nixwarden's entry shape, sopsFile relative to the repository), hostDrift (fleet.secretHosts that disagree with the model).";
        default =
          let
            modelHosts =
              lib.concatMap lib.attrValues (lib.attrValues config.fleet.guests)
              ++ lib.concatMap (s: lib.attrValues s.nodes) (lib.attrValues config.fleet.sites);
            modelKeys = lib.listToAttrs (
              map (x: {
                name = x.id;
                value = x.hostKeys;
              }) modelHosts
            );
            fileOf = ename: alias: estatesCfg.${ename}.secrets.files.${alias}.path or null;
            slotOut =
              x: v:
              if v == null || builtins.isString v then
                v
              else
                {
                  inherit (v) sopsKey;
                  sopsFile = if v.sopsFile == null then null else fileOf x.ename v.sopsFile;
                };
            identity = e: "${e.sopsFile}#${e.sopsKey}";
            sortEntries = lib.sort (a: b: identity a < identity b);
          in
          {
            sopsConfig = {
              inherit (config.fleet.sopsConfig) header protect;
              estates = map (
                n:
                let
                  l = estatesCfg.${n}.secrets.layout;
                in
                {
                  name = n;
                  inherit (l)
                    prefix
                    root
                    keysComment
                    rulesComment
                    ;
                  keys = map (k: { inherit (k) name age; }) l.keys;
                  rules = map (r: { inherit (r) pathRegex anchors; }) l.rules;
                  model = config.fleet.report.sops.${n};
                }
              ) config.fleet.sopsConfig.estates;
            };
            files = lib.concatLists (
              lib.mapAttrsToList (
                ename: e:
                lib.mapAttrsToList (fname: f: {
                  estate = ename;
                  file = fname;
                  inherit (f)
                    path
                    keys
                    readers
                    operators
                    ;
                }) e.secrets.files
              ) estatesCfg
            );
            placements = {
              infisical = sortEntries (
                map (x: {
                  estate = x.ename;
                  file = x.fname;
                  sopsFile = x.f.path;
                  sopsKey = x.key;
                  inherit (x.p.infisical)
                    project
                    folder
                    environment
                    groups
                    source
                    ;
                  name = infisicalName x;
                  hosts = x.f.readers;
                }) infisicalPlaced
              );
              bitwarden = sortEntries (
                map (
                  x:
                  let
                    b = x.p.bitwarden;
                  in
                  {
                    estate = x.ename;
                    file = x.fname;
                    sopsFile = x.f.path;
                    sopsKey = x.key;
                    inherit (b)
                      org
                      collection
                      type
                      uris
                      favorite
                      reprompt
                      ;
                    field = if b.field != null then b.field else placement.defaultField b.type;
                    name = bitwardenName x;
                    username = slotOut x b.username;
                    totp = slotOut x b.totp;
                    notes = slotOut x b.notes;
                    fields = map (n: {
                      name = n;
                      value = slotOut x b.fields.${n};
                    }) (lib.sort lib.lessThan (lib.attrNames b.fields));
                    hosts = x.f.readers;
                  }
                ) bitwardenPlaced
              );
            };
            hostDrift = lib.filter (x: x != null) (
              lib.mapAttrsToList (
                id: hk:
                let
                  m = modelKeys.${id} or null;
                in
                if m == null then
                  {
                    inherit id;
                    reason = "not a guest or node of the model";
                  }
                else if
                  (hk.age != null && hk.age != m.age)
                  || (hk.ed25519 != null && bareKey hk.ed25519 != bareKey m.ed25519)
                then
                  {
                    inherit id;
                    reason = "keys differ from the model's hostKeys";
                  }
                else
                  null
              ) config.fleet.secretHosts
            );
          };
      };

      anchorDrift = mkOption {
        type = types.attrsOf (
          types.submodule {
            options = {
              guestsWithoutAnchor = mkOption { type = types.listOf types.str; };
              extrasWithoutHost = mkOption { type = types.listOf types.str; };
              crossEstateHosts = mkOption { type = types.listOf types.str; };
            };
          }
        );
        readOnly = true;
        default = lib.mapAttrs (
          ename: e:
          let
            a = e.secrets.anchors;
            guestIds = estateGuestIds ename;
            otherGuestIds = lib.concatLists (
              lib.mapAttrsToList (n: _: estateGuestIds n) (lib.removeAttrs config.fleet.guests [ ename ])
            );
            re = lib.concatStringsSep ".+" (map lib.escapeRegex (lib.splitString "{name}" a.pattern));
          in
          {
            guestsWithoutAnchor = lib.filter (id: !lib.elem id a.rendered) guestIds;
            extrasWithoutHost = lib.filter (x: builtins.match re x != null) a.extra;
            crossEstateHosts = lib.filter (id: lib.elem id otherGuestIds) a.hosts;
          }
        ) config.fleet.estates;
        description = "Anchor drift per estate (data only, never an assertion): estate guests without an anchor, extras that look like hosts, hosts of other estates.";
      };

      sops = mkOption {
        type = types.attrsOf (
          types.submodule {
            options = {
              anchors = mkOption {
                type = types.attrsOf (
                  types.submodule {
                    options = {
                      kind = mkOption {
                        type = types.enum [
                          "host"
                          "operator"
                        ];
                      };
                      ssh = mkOption { type = types.nullOr types.str; };
                      age = mkOption { type = types.nullOr types.str; };
                      id = mkOption { type = types.nullOr types.str; };
                    };
                  }
                );
              };
              rules = mkOption {
                type = types.listOf (
                  types.submodule {
                    options = {
                      path = mkOption { type = types.str; };
                      anchors = mkOption { type = types.listOf types.str; };
                    };
                  }
                );
              };
              missingKeys = mkOption { type = types.listOf types.str; };
            };
          }
        );
        readOnly = true;
        default = lib.mapAttrs (
          _: e:
          let
            a = e.secrets.anchors;
            nodes = lib.concatMap (s: lib.attrValues s.nodes) (lib.attrValues config.fleet.sites);
            guests = lib.concatMap lib.attrValues (lib.attrValues config.fleet.guests);
            # id -> ssh host key (or null) for every guest and node.
            ageOf = lib.listToAttrs (
              map (x: {
                name = x.id;
                value = x.hostKeys.age;
              }) (guests ++ nodes)
            );
            keyOf = lib.listToAttrs (
              map (x: {
                name = x.id;
                value = x.hostKeys.ed25519;
              }) (guests ++ nodes)
            );
            anchorName = sopsAnchorName a;
            files = lib.attrValues e.secrets.files;
            readerIds = sopsReaderIds e;
            # The key may carry a trailing comment (root@host); only the type
            # and the key are passed on.
            bare = k: if k == null then null else lib.concatStringsSep " " (lib.take 2 (lib.splitString " " k));
            hostAnchors = lib.listToAttrs (
              map (id: {
                name = anchorName id;
                value = {
                  kind = "host";
                  ssh = bare (keyOf.${id} or null);
                  age = ageOf.${id} or null;
                  inherit id;
                };
              }) readerIds
            );
            operatorAnchors = lib.mapAttrs (_: o: {
              kind = "operator";
              ssh = null;
              inherit (o) age;
              id = null;
            }) e.secrets.operators;
          in
          {
            anchors = hostAnchors // operatorAnchors;
            rules = map (f: {
              inherit (f) path;
              anchors = lib.sort lib.lessThan (lib.unique (map anchorName f.readers ++ f.operators));
            }) files;
            missingKeys = lib.filter (
              id: (keyOf.${id} or null) == null && (ageOf.${id} or null) == null
            ) readerIds;
          }
        ) config.fleet.estates;
        description = "Derived, read-only: per estate, the sops recipient anchors (hosts that read a file, operators), one creation rule per secrets file and the readers that have neither a hostKeys.age nor a hostKeys.ed25519.";
      };
    };
  };
}
