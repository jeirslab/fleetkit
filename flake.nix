{
  description = "fleetkit: the schema and checks of the fleet model (no data; an estate repo supplies it)";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/b5aa0fbd538984f6e3d201be0005b4463d8b09f8";
    # ADR-0017: the secrets utility (lib.secrets, nixosModules.secrets) wires
    # hosts to sops-nix.
    sops-nix = {
      url = "github:Mic92/sops-nix";
      inputs.nixpkgs.follows = "nixpkgs";
    };
  };

  outputs =
    {
      self,
      nixpkgs,
      sops-nix,
      ...
    }:
    let
      inherit (nixpkgs) lib;
      linuxSystems = [
        "x86_64-linux"
        "aarch64-linux"
      ];
      # The secrets CLI and a data repository's check also run on darwin.
      secretsSystems = linuxSystems ++ [
        "x86_64-darwin"
        "aarch64-darwin"
      ];
      pkgsFor = system: nixpkgs.legacyPackages.${system};

      # ADR-0009, ADR-0017: the secrets utility. The schema
      # (modules/secrets.nix) is part of every mkFleet; sopsRef resolves
      # sops:<estate>/<file>#<key> for the renderers; mkData is a secrets data
      # repository's whole flake (docs/registry.md).
      secretsRuntime =
        pkgs: with pkgs; [
          python3
          bash
          coreutils
          gnugrep
          gnused
          gawk
          diffutils
          git
          ripgrep
          sops
          age
          ssh-to-age
          openssh
          openssl
        ];
      # The secrets CLI and the tools it runs (tools/secrets calls the others
      # beside it).
      secretsTools = lib.fileset.toSource {
        root = ./.;
        fileset = lib.fileset.unions [
          ./tools/secrets
          ./tools/secrets-check.py
          ./tools/secrets-refs.py
          ./tools/sops-config.py
          ./tools/deploy-key
          ./tools/github-app-bootstrap
          ./tools/hooks/secrets-scan.sh
        ];
      };
      secrets = {
        # ADR-0009, ADR-0017: a secrets data repository's whole flake
        # (lib/secrets/mk-data.nix): { root; registry; sync ? { }; allow ? { }; }.
        mkData = import ./lib/secrets/mk-data.nix {
          inherit lib pkgsFor self;
          systems = secretsSystems;
        };
        # Evaluate a registry on its own: { config; options; failures; ok; guard; checkReport; }.
        evalRegistry = import ./lib/secrets/eval-registry.nix { inherit lib; };
        # The schema (fleet.estates.<e>.secrets, fleet.sopsConfig,
        # fleet.secretHosts, fleet.report.{sops,anchorDrift,secrets}); mkFleet
        # already has it.
        schemaModule = ./modules/secrets.nix;
        # The stand-in for the fleet model that evalRegistry adds.
        standaloneModule = ./lib/secrets/standalone.nix;
        # sops:<estate>/<file>#<key> -> { name; path; key; extract; expr; }.
        sopsRef = import ./lib/secrets/sops-ref.nix;
        # The tools' directory (stdlib Python and bash).
        tools = "${secretsTools}/tools";
      };
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
        # ADR-0009, ADR-0017: the secrets utility: mkData, evalRegistry,
        # schemaModule, standaloneModule, sopsRef, tools (docs/registry.md).
        inherit secrets;
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

      nixosModules = {
        # Experimental: the deploy server as a NixOS service (GitOps mode).
        fleetkit-server = import ./nixos/fleetkit-server.nix { inherit self; };
        # ADR-0009, ADR-0017: sops-nix plus the SSH host key as the identity a
        # host decrypts with.
        secrets = import ./nixos/secrets.nix sops-nix;
        # addons/README.md: NixOS-module add-ons under fleetkit.addons.<name>,
        # all off until switched on. `addons` imports every one.
        addons = ./addons/nix;
        addons-tailscale = ./addons/nix/pkgs/tailscale;
      };

      # Experimental: the deploy runner (cli/). pulumi, colmena and sops are on
      # its PATH; nix is the host's.
      packages = lib.genAttrs secretsSystems (
        system:
        let
          pkgs = pkgsFor system;
        in
        {
          # ADR-0009, ADR-0017: the secrets CLI (check, render, scan, edit,
          # host add, rekey, refs, import, deploy-key, app-bootstrap).
          secrets = pkgs.stdenvNoCC.mkDerivation {
            pname = "secrets";
            version = "0.1.0";
            src = secretsTools;
            nativeBuildInputs = [ pkgs.makeWrapper ];
            dontBuild = true;
            installPhase = ''
              runHook preInstall
              mkdir -p $out/share/fleetkit-secrets $out/bin
              cp -r tools $out/share/fleetkit-secrets/
              makeWrapper ${pkgs.python3}/bin/python3 $out/bin/secrets \
                --add-flags $out/share/fleetkit-secrets/tools/secrets \
                --prefix PATH : ${lib.makeBinPath (secretsRuntime pkgs)}
              runHook postInstall
            '';
            meta = {
              description = "The secrets command line: check, render, scan, edit, host add, rekey, refs, import, deploy-key, app-bootstrap";
              mainProgram = "secrets";
            };
          };
        }
        // lib.optionalAttrs (lib.elem system linuxSystems) (
          let
            pkgs = nixpkgs.legacyPackages.${system};
            py = pkgs.python3Packages;
            # The Ansible add-on's playbook runs (docs/addons.md): ansible-core
            # and the community package's collections (vyos.vyos,
            # ansible.netcommon, ...), all from this nixpkgs; nothing is
            # fetched by ansible-galaxy at deploy time. The collections it has
            # are listed in providers/ansible/ (tests/gen_ansible_collections.py).
            ansible = pkgs.python3.withPackages (ps: [
              ps.ansible-core
              ps.ansible
            ]);
            ansibleBin = pkgs.runCommand "fleetkit-ansible" { } ''
              mkdir -p $out/bin
              for b in ${ansible}/bin/ansible*; do ln -s "$b" $out/bin/; done
            '';
          in
          rec {
            inherit ansible;
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
                  # builds the venv of the repo's custom tools (docs/tools.md)
                  pkgs.uv
                ])
                # The ansible/ansible provider runs `ansible-playbook` from PATH:
                # this one, ahead of any the host has. Only its ansible-*
                # commands, not the environment's python.
                "--prefix"
                "PATH"
                ":"
                "${ansibleBin}/bin"
                # Collections from the environment only: not ones a user of the
                # host installed under ~/.ansible (a playbook's own collections/
                # beside it are still read).
                "--set"
                "ANSIBLE_COLLECTIONS_PATH"
                "${ansible}/${pkgs.python3.sitePackages}"
              ];
              meta.mainProgram = "fleetkit";
            };
            default = fleetkit;
          }
        )
      );

      # ADR-0009, ADR-0017: the secrets CLI and what it runs (a data
      # repository's `nix develop`).
      devShells = lib.genAttrs secretsSystems (
        system:
        let
          pkgs = pkgsFor system;
        in
        {
          secrets = pkgs.mkShell {
            packages = [ self.packages.${system}.secrets ] ++ secretsRuntime pkgs;
          };
        }
      );

      templates.data = {
        path = ./templates/data;
        description = "A secrets data repository: registry.nix, hosts.json, .sops.yaml, one fleetkit.lib.secrets.mkData call";
      };

      # The schema's own description of itself, from a model with no data
      # (tests/guest_fidelity.py and tests/loose_blocks.sh read it).
      fleet.report = { inherit (empty.config.fleet.report) guestOptionPaths looseBlocks; };
    };
}
