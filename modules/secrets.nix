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
        files = mkOption {
          type = types.lazyAttrsOf (fileType estate);
          default = { };
        };
      };
    };

  dups = keys: lib.filter (k: lib.count (x: x == k) keys > 1) (lib.unique keys);

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
      ++ h.refAssertions {
        where = "fleet.estates.${ename}.secrets.recipients";
        kind = "anchor";
        ids = a.all;
      } e.secrets.recipients
    ) config.fleet.estates
  );

  options.fleet.report.anchorDrift = mkOption {
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
}
