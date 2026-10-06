# fleet.estates.<estate>.secrets: the sops file registry. Each file declares
# the key NAMES it holds; `ref.<key>` is derived as "sops:<estate>/<file>#<key>"
# for every declared key and for nothing else, so `files.<f>.ref."<undeclared>"` is an
# evaluation error ("attribute '<undeclared>' missing"). Values are never
# read.
{ lib, config, ... }:
let
  inherit (lib) types mkOption;
  h = import ./lib.nix { inherit lib; };

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
          lib.replaceStrings [ "{name}" ] [
            (config.aliases.${id} or (lib.replaceStrings [ "-" ] [ "_" ] (lib.last (lib.splitString "/" id))))
          ] config.pattern;
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
          ref = mkOption {
            type = types.lazyAttrsOf types.str;
            readOnly = true;
            default = lib.genAttrs config.keys (k: "sops:${estate}/${name}#${k}");
            description = "ref.<key> = \"sops:<estate>/<file>#<key>\" for every declared key; any other key is an evaluation error.";
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
        assertion = lib.all (k: k == "" || builtins.match "[A-Za-z0-9_.-]+(/[A-Za-z0-9_.-]+)*" k != null) f.keys;
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
    lib.concatLists (
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

  options.fleet.report = {
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
          missingKeys = lib.filter (id: (keyOf.${id} or null) == null && (ageOf.${id} or null) == null) readerIds;
        }
      ) config.fleet.estates;
      description = "Derived, read-only: per estate, the sops recipient anchors (hosts that read a file, operators), one creation rule per secrets file and the readers that have neither a hostKeys.age nor a hostKeys.ed25519.";
    };
  };
}
