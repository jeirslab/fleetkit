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
        # -> one NixOS system per guest that names a nixos.module. With `site`
        # instead of `estate`: one per node of that site that names one.
        mkSystems = import ./lib/systems.nix;
        # { fleet; estate; nixpkgs; modules; specialArgs; network; system; }
        # -> a Colmena hive: meta plus one node per guest that names a nixos.module.
        # With `site` instead of `estate`: the site's nodes that name one.
        mkHive = import ./lib/hive.nix;
        # { fleet; estate; } -> an attrset for builtins.toJSON into main.tf.json.
        mkTerraform = args: import ./lib/terraform.nix ({ inherit (nixpkgs) lib; } // args);
        # { fleet; estate; } -> the estate's GitHub repositories and organisation
        # as an attrset for builtins.toJSON into main.tf.json.
        mkGithubTerraform = args: import ./lib/github.nix ({ inherit (nixpkgs) lib; } // args);
        # Experimental: the same renders as Pulumi YAML programs (builtins.toJSON
        # into Pulumi.yaml), through Pulumi's terraform-provider bridge at the
        # pinned provider versions. See docs/pulumi.md.
        # { tf; project; description ? null; } -> a Pulumi YAML program.
        toPulumi = args: import ./lib/pulumi.nix ({ inherit (nixpkgs) lib; } // args);
        # { fleet; estate; adopt ? false; } -> mkTerraform's render as a Pulumi
        # program. adopt = true sets options.import on every guest and pool
        # (bpg import ids <node>/<vmid> and <pool_id>), for moving an estate
        # that is already deployed onto Pulumi without recreating it.
        mkPulumi =
          {
            adopt ? false,
            ...
          }@args:
          import ./lib/pulumi.nix {
            inherit (nixpkgs) lib;
            tf = import ./lib/terraform.nix ({ inherit (nixpkgs) lib; } // removeAttrs args [ "adopt" ]);
            project = "${args.estate}-guests";
            adopt = nixpkgs.lib.optionalAttrs adopt (
              let
                guest = _: a: "${a.node_name}/${toString a.vm_id}";
              in
              {
                proxmox_virtual_environment_container = guest;
                proxmox_virtual_environment_vm = guest;
                proxmox_virtual_environment_pool = _: a: a.pool_id;
              }
            );
          };
        # { fleet; estate; } -> mkGithubTerraform's render as a Pulumi program.
        mkGithubPulumi =
          args:
          import ./lib/pulumi.nix {
            inherit (nixpkgs) lib;
            tf = import ./lib/github.nix ({ inherit (nixpkgs) lib; } // args);
            project = "${args.estate}-github";
          };
      };

      # The schema's own description of itself, from a model with no data
      # (tests/guest_fidelity.py and tests/loose_blocks.sh read it).
      fleet.report = { inherit (empty.config.fleet.report) guestOptionPaths looseBlocks; };
    };
}
