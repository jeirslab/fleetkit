{
  description = "fleetkit: the schema and checks of the fleet model (no data; an estate repo supplies it)";

  # Only nixpkgs.lib is used: no systems, no packages, nothing to build.
  inputs.nixpkgs.url = "github:NixOS/nixpkgs/b5aa0fbd538984f6e3d201be0005b4463d8b09f8";

  outputs =
    { self, nixpkgs, ... }:
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
        # Pulumi.nix: an estate's Pulumi stacks as Nix modules, each property
        # typed from the pinned schemas. { stacks; fromModel; } (lib/pulumi).
        pulumi = import ./lib/pulumi {
          inherit (nixpkgs) lib;
          inherit self;
        };
        # The provider arguments of an estate, in the pinned Terraform
        # providers' own names (Pulumi runs those providers through its bridge,
        # and their schemas are what the model is checked against). Internal:
        # the stage mkPulumi / mkGithubPulumi compile from; tests read it.
        internal = {
          # { fleet; estate; } -> guests and pools, main.tf.json shaped.
          guests = args: import ./lib/terraform.nix ({ inherit (nixpkgs) lib; } // args);
          # { fleet; estate; } -> the GitHub organisation and repositories.
          github = args: import ./lib/github.nix ({ inherit (nixpkgs) lib; } // args);
        };
        # Pulumi YAML programs (builtins.toJSON into Pulumi.yaml) through
        # Pulumi's terraform-provider bridge at the pinned provider versions;
        # the fleetkit package runs them, then Colmena. See docs/pulumi.md.
        # { tf; project; description ? null; } -> a Pulumi YAML program.
        toPulumi = args: import ./lib/pulumi.nix ({ inherit (nixpkgs) lib; } // args);
        # { fleet; estate; adopt ? false; } -> the estate's guests and pools as a
        # Pulumi program. adopt = true sets options.import on every guest and pool
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
        # { fleet; estate; } -> the estate's GitHub organisation as a Pulumi program.
        mkGithubPulumi =
          args:
          import ./lib/pulumi.nix {
            inherit (nixpkgs) lib;
            tf = import ./lib/github.nix ({ inherit (nixpkgs) lib; } // args);
            project = "${args.estate}-github";
          };
        # The same two stages under their Terraform-era names: tests written
        # against unstable (tests/github.sh, tests/terraform.sh) read these.
        # { fleet; estate; } -> an attrset for builtins.toJSON into main.tf.json.
        mkTerraform = args: import ./lib/terraform.nix ({ inherit (nixpkgs) lib; } // args);
        # { fleet; estate; } -> the estate's GitHub repositories and organisation
        # as an attrset for builtins.toJSON into main.tf.json.
        mkGithubTerraform = args: import ./lib/github.nix ({ inherit (nixpkgs) lib; } // args);
        # { org; tiers ? [ "pipeline" "terraform-admin" ]; name ? null; redirectUrl ? null; }
        # -> the GitHub App manifest for that org (docs/github-app.md).
        mkGithubAppManifest = args: import ./lib/github-app.nix ({ inherit (nixpkgs) lib; } // args);
        # { name; workflow; ref; on; with ? { }; secrets ? { }; permissions ? { }; kitRepo ? "jeirslab/fleetkit"; }
        # -> the text of a caller workflow file that runs the kit's reusable workflow
        # at `ref` (a 40-hex commit SHA, else an evaluation error).
        mkWorkflowCaller = args: import ./lib/workflow-caller.nix ({ inherit (nixpkgs) lib; } // args);
      };

      # Experimental: the deploy server as a NixOS service (GitOps mode).
      nixosModules.fleetkit-server = import ./nixos/fleetkit-server.nix { inherit self; };

      # Experimental: the deploy runner (cli/). pulumi, colmena and sops are on
      # its PATH; nix is the host's.
      packages = nixpkgs.lib.genAttrs [ "x86_64-linux" "aarch64-linux" ] (
        system:
        let
          pkgs = nixpkgs.legacyPackages.${system};
          py = pkgs.python3Packages;
        in
        rec {
          fleetkit = py.buildPythonApplication {
            pname = "fleetkit";
            version = "0.1.0";
            pyproject = true;
            src = ./cli;
            build-system = [ py.setuptools ];
            dependencies = with py; [
              click
              fastapi
              uvicorn
              pydantic
              pulumi
            ];
            nativeCheckInputs = [
              py.pytestCheckHook
              py.httpx
              pkgs.git
            ];
            makeWrapperArgs = [
              "--suffix"
              "PATH"
              ":"
              (nixpkgs.lib.makeBinPath [
                pkgs.pulumi-bin
                pkgs.colmena
                pkgs.sops
                pkgs.git
              ])
            ];
            meta.mainProgram = "fleetkit";
          };
          default = fleetkit;
        }
      );

      # The schema's own description of itself, from a model with no data
      # (tests/guest_fidelity.py and tests/loose_blocks.sh read it).
      fleet.report = { inherit (empty.config.fleet.report) guestOptionPaths looseBlocks; };
    };
}
