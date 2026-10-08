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
      adoptRules = import ./lib/pulumi/adopt.nix { inherit (nixpkgs) lib; };
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
        # { tf; project; description ? null; adopt ? { }; } -> a Pulumi YAML program.
        toPulumi = args: import ./lib/pulumi.nix ({ inherit (nixpkgs) lib; } // args);
        # { fleet; estate; adopt ? false; } -> the estate's guests and pools as a
        # Pulumi program. With adopt = true every guest and pool also carries
        # `adopt`, the id the provider imports it by (<node>/<vmid>, <pool_id>;
        # lib/pulumi/adopt.nix): the Pulumi.nix resource shape that
        # lib.pulumi.fromModel feeds to lib.pulumi.stacks, not a Pulumi.yaml.
        # `import` is never rendered (fleetkit#61).
        mkPulumi =
          {
            adopt ? false,
            ...
          }@args:
          import ./lib/pulumi.nix {
            inherit (nixpkgs) lib;
            tf = import ./lib/terraform.nix ({ inherit (nixpkgs) lib; } // removeAttrs args [ "adopt" ]);
            project = "${args.estate}-guests";
            adopt = nixpkgs.lib.optionalAttrs adopt adoptRules.proxmox;
          };
        # { fleet; estate; adopt ? false; } -> the estate's GitHub organisation
        # as a Pulumi program. adopt = true as for mkPulumi; a resource whose
        # id the model does not determine carries `adoptUnresolved` instead.
        mkGithubPulumi =
          {
            adopt ? false,
            ...
          }@args:
          let
            tf = import ./lib/github.nix ({ inherit (nixpkgs) lib; } // removeAttrs args [ "adopt" ]);
          in
          import ./lib/pulumi.nix {
            inherit (nixpkgs) lib;
            inherit tf;
            project = "${args.estate}-github";
            adopt = nixpkgs.lib.optionalAttrs adopt (adoptRules.github tf);
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
              # tests/test_real_pulumi.py runs the real engine, offline (a file
              # backend, the random and tls providers that ship beside it).
              pkgs.pulumi-bin
            ];
            # The guest list is checked against the pinned provider's name map
            # (tests/test_guests.py), which is not under cli/.
            preCheck = ''
              export FLEETKIT_PROVIDER_NAMES=${./providers/pulumi/names}
              export HOME=$TMPDIR
            '';
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
