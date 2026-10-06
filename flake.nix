{
  description = "fleetkit: the schema and checks of the fleet model (no data; an estate repo supplies it)";

  # Only nixpkgs.lib is used: no systems, no packages, nothing to build.
  inputs.nixpkgs.url = "github:NixOS/nixpkgs/b5aa0fbd538984f6e3d201be0005b4463d8b09f8";

  outputs =
    { nixpkgs, ... }:
    let
      fleetLib = import ./lib;
      withLib = f: args: f ({ inherit (nixpkgs) lib; } // args);
      empty = fleetLib.mkFleet { inherit (nixpkgs) lib; };
    in
    {
      # mkFleet / fleet take { modules; tenants ? { }; }; nixpkgs' lib is
      # supplied here (pass `lib` to override it).
      lib = {
        inherit (fleetLib) checked tenantOwned;
        mkFleet = withLib fleetLib.mkFleet;
        fleet = withLib fleetLib.fleet;
        # { fleet; estate; nixpkgs; modules; specialArgs; adminPrincipals; }
        # -> one NixOS system per guest that names a nixos.module.
        mkSystems = import ./lib/systems.nix;
        # { fleet; estate; nixpkgs; modules; specialArgs; network; system; }
        # -> a Colmena hive: meta plus one node per guest that names a nixos.module.
        mkHive = import ./lib/hive.nix;
        # { fleet; estate; } -> an attrset for builtins.toJSON into main.tf.json.
        mkTerraform = args: import ./lib/terraform.nix ({ inherit (nixpkgs) lib; } // args);
        # { fleet; estate; } -> the estate's GitHub repositories and organisation
        # as an attrset for builtins.toJSON into main.tf.json.
        mkGithubTerraform = args: import ./lib/github.nix ({ inherit (nixpkgs) lib; } // args);
      };

      # The schema's own description of itself, from a model with no data
      # (tests/guest_fidelity.py and tests/loose_blocks.sh read it).
      fleet.report = { inherit (empty.config.fleet.report) guestOptionPaths looseBlocks; };
    };
}
