# ADR-0009, ADR-0017: where a registry key's value is mirrored, beside the key.
#
#   files.<f>.placements.<key> = {
#     infisical = { project; folder ? "/"; environment ? "prod"; name ? null; groups ? [ ]; source ? "sops"; };
#     bitwarden = { org; collection; type ? "login"; field ? null; name ? null; username ? null;
#                   uris ? [ ]; totp ? null; notes ? null; fields ? { }; favorite ? false; reprompt ? false; };
#   };
#
# The fields are those of nixfisical's lib.mkInfisical and nixwarden's
# lib.mkBitwarden, so the result of either call can be assigned as is. A slot of
# a Bitwarden item (username, totp, notes, a custom field) is a literal string
# or a reference { sopsKey; sopsFile ? null; } to another key: sopsFile is the
# alias of a file of the same estate (secrets.files.<alias>), null for the
# entry's own file. Not a module: modules/secrets.nix imports it.
{ lib }:
let
  inherit (lib) types mkOption;

  # nixwarden's item kinds and the slots each accepts (nixwarden nix/lib).
  bitwardenTypes = [
    "login"
    "note"
    "ssh-key"
  ];
  bitwardenFields = [
    "password"
    "username"
    "totp"
    "notes"
    "privateKey"
  ];
  defaultField =
    type:
    {
      login = "password";
      note = "notes";
      ssh-key = "privateKey";
    }
    .${type};
  fieldsFor =
    type:
    {
      login = [
        "password"
        "username"
        "totp"
        "notes"
      ];
      note = [ "notes" ];
      ssh-key = [
        "privateKey"
        "notes"
      ];
    }
    .${type};

  refType = types.submodule {
    options = {
      sopsKey = mkOption {
        type = types.str;
        description = "A declared key.";
      };
      sopsFile = mkOption {
        type = types.nullOr types.str;
        default = null;
        description = "Alias of a file of the same estate (secrets.files.<alias>); null: the entry's own file.";
      };
    };
  };
  slot = types.nullOr (types.either types.str refType);

  infisicalType = types.submodule {
    options = {
      project = mkOption {
        type = types.str;
        description = "Infisical project (the access boundary; no default).";
      };
      folder = mkOption {
        type = types.str;
        default = "/";
      };
      environment = mkOption {
        type = types.str;
        default = "prod";
      };
      name = mkOption {
        type = types.nullOr types.str;
        default = null;
        description = "Secret name in Infisical; null: the last segment of the key.";
      };
      groups = mkOption {
        type = types.listOf types.str;
        default = [ ];
      };
      source = mkOption {
        type = types.enum [
          "sops"
          "infisical"
        ];
        default = "sops";
        description = "Which side owns the value (nixfisical): sops pushes, infisical pulls.";
      };
    };
  };

  bitwardenType = types.submodule {
    options = {
      org = mkOption { type = types.str; };
      collection = mkOption { type = types.str; };
      type = mkOption {
        type = types.enum bitwardenTypes;
        default = "login";
      };
      field = mkOption {
        type = types.nullOr (types.enum bitwardenFields);
        default = null;
        description = "Slot the key's value lands in; null: the type's own (password, notes, privateKey).";
      };
      name = mkOption {
        type = types.nullOr types.str;
        default = null;
        description = "Item name; null: the last segment of the key.";
      };
      username = mkOption {
        type = slot;
        default = null;
      };
      uris = mkOption {
        type = types.listOf types.str;
        default = [ ];
      };
      totp = mkOption {
        type = slot;
        default = null;
      };
      notes = mkOption {
        type = slot;
        default = null;
      };
      fields = mkOption {
        type = types.attrsOf (types.either types.str refType);
        default = { };
      };
      favorite = mkOption {
        type = types.bool;
        default = false;
      };
      reprompt = mkOption {
        type = types.bool;
        default = false;
      };
    };
  };

  lastSeg = k: lib.last (lib.splitString "/" k);
  isRef = v: builtins.isAttrs v;
in
{
  inherit
    bitwardenTypes
    bitwardenFields
    defaultField
    fieldsFor
    lastSeg
    isRef
    ;

  type = types.submodule {
    options = {
      infisical = mkOption {
        type = types.nullOr infisicalType;
        default = null;
      };
      bitwarden = mkOption {
        type = types.nullOr bitwardenType;
        default = null;
      };
    };
  };

  # The slots of a Bitwarden entry that may hold a reference, with their names.
  refSlots =
    b:
    lib.filter (x: isRef x.value) (
      [
        {
          name = "username";
          value = b.username;
        }
        {
          name = "totp";
          value = b.totp;
        }
        {
          name = "notes";
          value = b.notes;
        }
      ]
      ++ lib.mapAttrsToList (n: v: {
        name = "fields.${n}";
        value = v;
      }) b.fields
    );

  infisicalCoordinate = i: name: "${i.project}/${i.environment}${i.folder}:${name}";
  bitwardenCoordinate = b: name: "${b.org}/${b.collection}:${name}";
}
