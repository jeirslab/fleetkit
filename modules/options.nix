# Top-level module of the fleet schema. It declares the assertion list, the
# fleetkit.* tool settings and the fleet.report.* derived views, and imports
# one module per block. Every block module owns its own options and
# assertions; shared helpers live in ./lib.nix (not a module).
{ lib, config, ... }:
let
  inherit (lib) types mkOption;
  h = import ./lib.nix { inherit lib; };

  modes = [
    "inspect"
    "plan"
    "apply"
  ];
  rank = m: lib.lists.findFirstIndex (x: x == m) 0 modes;

  ipClaim = types.submodule {
    options = {
      ip = mkOption { type = types.str; };
      claimants = mkOption { type = types.listOf types.str; };
    };
  };
  vmidClaim = types.submodule {
    options = {
      cluster = mkOption { type = types.str; };
      vmid = mkOption { type = types.int; };
      claimants = mkOption { type = types.listOf types.str; };
      reason = mkOption { type = types.str; };
    };
  };
in
{
  imports = [
    ./sites.nix
    ./estates.nix
    ./secrets.nix
    ./loose.nix
    ./zones.nix
    ./backends.nix
    ./operators.nix
    ./repos.nix
    ./guests.nix
  ];

  options = {
    assertions = mkOption {
      type = types.listOf (
        types.submodule {
          options = {
            assertion = mkOption { type = types.bool; };
            message = mkOption { type = types.str; };
          };
        }
      );
      default = [ ];
      internal = true;
      description = "Model invariants. lib/default.nix `checked` throws if any is false.";
    };

    fleetkit = {
      mode = mkOption {
        type = types.enum modes;
        default = "inspect";
        description = "Default operating mode of the fleet CLI. A flag may lower it, never raise it.";
      };
      modeMax = mkOption {
        type = types.enum modes;
        default = "plan";
        description = "Ceiling for `mode`.";
      };
      keys = {
        ageDir = mkOption { type = types.str; };
        sshDir = mkOption { type = types.str; };
      };
      sops.defaultFormat = mkOption {
        type = types.enum [
          "json"
          "yaml"
        ];
        default = "json";
      };
    };

    fleet.report = {
      ids = mkOption {
        type = types.attrsOf (types.listOf types.str);
        readOnly = true;
        default = h.index config.fleet;
        description = "Every id of every kind. Reference assertions check against these lists.";
      };

      looseBlocks = mkOption {
        type = types.listOf types.str;
        readOnly = true;
        default = [
          "fleet.estates.<estate>.git"
          "fleet.estates.<estate>.cache"
          "fleet.estates.<estate>.build"
          "fleet.estates.<estate>.observability"
          "fleet.estates.<estate>.substrate"
          "fleet.estates.<estate>.gpu"
          "fleet.estates.<estate>.llm"
          "fleet.estates.<estate>.storage"
          "fleet.estates.<estate>.colmena"
        ];
        description = "Option paths whose contents are not fully typed. Inventoried in docs/schema-todo.md.";
      };

      ipCollisions = mkOption {
        type = types.listOf ipClaim;
        readOnly = true;
        description = "IPv4 addresses claimed by more than one guest/node/router (data, not an error). Defined in guests.nix.";
      };

      vmidCollisions = mkOption {
        type = types.listOf vmidClaim;
        readOnly = true;
        description = "vmid collisions inside one cluster that are allowed by fleet.allow.vmidCollisions. Defined in guests.nix.";
      };
    };
  };

  config.assertions = [
    {
      assertion = rank config.fleetkit.mode <= rank config.fleetkit.modeMax;
      message = "fleetkit.mode \"${config.fleetkit.mode}\" exceeds fleetkit.modeMax \"${config.fleetkit.modeMax}\"";
    }
  ];
}
