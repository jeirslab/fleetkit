{
  # fleetkit — reusable fleet provisioning + deployment framework
  # (ADR-092 / INFRA-218, extracted from a production fleet deployment repo).
  #
  # The framework owns MACHINERY: the fleet manifest schema, terranix
  # emitters (Proxmox / Xen Orchestra / DNS), the Colmena/NixOS host
  # assembly, generic NixOS modules, bootstrap images, and the `fleet`
  # operator CLI. It owns NO environment data: hosts, providers,
  # networks, identities, DNS zones, secrets, and app flakes all live
  # in a CONSUMER repo, which calls `fleetkit.lib.mkFleet` with its
  # manifest modules and re-exports the result as its own flake
  # outputs. See templates/minimal for the consumer skeleton.

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    flake-utils.url = "github:numtide/flake-utils";
    # Terranix emits Terraform JSON from Nix modules.
    terranix = {
      url = "github:terranix/terranix";
      inputs.nixpkgs.follows = "nixpkgs";
    };
    sops-nix = {
      url = "github:Mic92/sops-nix";
      inputs.nixpkgs.follows = "nixpkgs";
    };
    # Build-time disk formatter for the bootstrap images (never re-runs
    # at activation, so it can't wipe live machines on rebuild).
    disko = {
      url = "github:nix-community/disko";
      inputs.nixpkgs.follows = "nixpkgs";
    };
    # Automated topology diagrams (SVG) rendered from the fleet manifest.
    nix-topology = {
      url = "github:oddlama/nix-topology";
      inputs.nixpkgs.follows = "nixpkgs";
    };
    # One golden config → many bootstrap image formats (proxmox-lxc, proxmox
    # VM, raw-efi, docker). Folded in from fleetkit-deployer as the images
    # component family; used only by nix/images/deployer, never the eval path.
    nixos-generators = {
      url = "github:nix-community/nixos-generators";
      inputs.nixpkgs.follows = "nixpkgs";
      inputs.nixlib.follows = "nixpkgs";
    };
  };

  outputs = { self, nixpkgs, flake-utils, terranix, sops-nix, disko, nix-topology, nixos-generators }:
  let
    nixLib = import ./nix/lib { inherit nixpkgs; };

    # ── The framework entry point ─────────────────────────────────
    #
    # fleetkit.lib.mkFleet {
    #   modules            = [ ./fleet ];        # manifest data: hosts, providers,
    #                                            # network, users, dns, fleet.settings
    #   backend            = { bucket = "my-tofu-state"; };   # tofu state (S3)
    #   globalModules      = [ ... ];            # NixOS modules for every host
    #                                            # (app flakes, _module.args, ...)
    #   hostExtraModules   = { host = [ ... ]; };# flake-input modules for one host
    #   colmenaOverlays    = [ ... ];            # nixpkgs overlays for colmena meta
    #   system             = "x86_64-linux";
    #   sopsAgeKeyCommand  = [ ... ];            # colmena sops key lookup
    # }
    # → { fleetEval, hosts, deployable, colmena, nixosConfigurations,
    #     packages (hostsJson / tf-stack-ids / tf-<slug>… / run utils),
    #     fleetManifest, fleetAccess }
    #
    # The consumer re-exports these as its own flake outputs, so
    # `colmena`, `nix build .#tf-<slug>`, and the fleet CLI's
    # contracts all keep working unchanged.
    mkFleet = {
      modules,
      # Tofu state backend ({ bucket, region ? }). Optional since ADR-097:
      # when omitted it comes from `fleet.settings.backend` — the flake
      # stays bootstrap-only and the bucket is declared once, in Nix.
      backend ? null,
      globalModules ? [],
      hostExtraModules ? {},
      # Extra RAW terranix modules per stack ({ "<env>.<stack>" = [ module ]; }).
      # The escape hatch for provider families fleetkit does not model
      # (a router's uci provider, a one-off SaaS resource): the module is
      # appended to that stack's terranix eval only — it never enters the
      # fleet-schema eval, so plain `resource.*`/`provider.*` config is fine.
      tfExtraModules ? {},
      # Deep-merged into colmena's meta — for per-node package sets
      # (meta.nodeNixpkgs, e.g. one CUDA host) and similar colmena-only
      # knobs mkFleet has no first-class argument for.
      colmenaMeta ? {},
      # Extra specialArgs for every host eval (nixosConfigurations AND
      # colmena). Use for values that must be visible during module
      # IMPORT resolution — e.g. `inputs` when host modules do
      # `imports = [ "''${inputs.nixpkgs}/nixos/modules/..." ]`.
      # (_module.args is config-stage only; it cannot feed imports.)
      specialArgs ? {},
      colmenaOverlays ? [],
      system ? "x86_64-linux",
      sopsAgeKeyCommand ? [ "sh" "-c" "cat \"$HOME/.ssh/sops-age.key\"" ],
      # Path to the consumer's SOPS store (scaffold it with `fleet
      # secrets init`). Wired into sops.defaultSopsFile on every host so
      # modules' sopsLib.mkSecret declarations resolve without any
      # per-consumer sops plumbing. null = consumer wires sops itself
      # (or runs a fleet with no secrets — most modules won't).
      secretsFile ? null,
    }:
    let
      pkgs = import nixpkgs { inherit system; };

      # Single fleet eval — the source of truth for every host's
      # vm_id / ip / internal_ip / tags / kind and the leaf stacks.
      # fleetLib is injected into the MANIFEST eval too, not just into NixOS
      # hosts: consumer manifest modules (Grafana Cloud checks, PVE notes)
      # need the same builders at fleet-eval time, where no NixOS module
      # argument exists yet.
      fleetLib = import ./nix/lib/module-args.nix { lib = nixpkgs.lib; inherit pkgs nixpkgs nixos-generators; };

      fleetEval = (nixpkgs.lib.evalModules {
        modules = [ ./nix/fleet { _module.args.fleetLib = fleetLib; } ] ++ modules;
      }).config.fleet;

      # ADR-097: backend argument > fleet.settings.backend, loudly none.
      # `perStack` rides along on both paths: nix/tf resolves it per stack,
      # and dropping it here would silently ignore every override (the
      # reconstruction below is explicit, so a new field must be added by
      # hand — found the hard way).
      backendPerStack = fleetEval.settings.backend.perStack or { };
      backend' =
        if backend != null then
          { perStack = backendPerStack; } // backend
        else if fleetEval.settings.backend.type == "local" then {
          type = "local";
          perStack = backendPerStack;
        }
        else if fleetEval.settings.backend.type == "pg" then {
          type = "pg";
          pgSchemaPrefix = fleetEval.settings.backend.pg.schemaPrefix;
          perStack = backendPerStack;
        }
        else if fleetEval.settings.backend.bucket != null then {
          type = "s3";
          bucket = fleetEval.settings.backend.bucket;
          region = fleetEval.settings.backend.region;
          pgSchemaPrefix = fleetEval.settings.backend.pg.schemaPrefix;
          perStack = backendPerStack;
        }
        else throw "mkFleet: no tofu state backend — set fleet.settings.backend.bucket (or backend.type = \"local\", or pass mkFleet { backend = ...; })";

      hosts =
        let
          base = nixLib.mkHosts {
            hosts    = fleetEval.hostsRegistry;
            runtime  = fleetEval.hostsJson;
            helpers  = {
              dnsRecords           = fleetEval.dnsRecords;
              publicDnsRecords     = fleetEval.publicDnsRecords;
              dnsRecordsByProvider = fleetEval.dnsRecordsByProvider;
            };
          };
        in
        nixpkgs.lib.mapAttrs
          (name: h: h // { modules = h.modules ++ (hostExtraModules.${name} or []); })
          base;

      deployable = nixpkgs.lib.filterAttrs (_: h: h ? targetHost) hosts;

      # NixOS modules every host gets: the framework's generic modules,
      # the fleet schema + the consumer's manifest data (so any module
      # can read config.fleet.hostsJson / .network / .settings), sops,
      # and whatever the consumer passes.
      baseGlobalModules = [
        sops-nix.nixosModules.sops
        disko.nixosModules.disko
        ./nix/modules
        ./nix/fleet
        # fleetkit's public helper surface, so CONSUMER modules can reach the
        # same builders framework modules use (sops secret declaration, grafana
        # dashboards, PVE notes, …) without vendoring a copy or path-importing
        # into this flake's store path. See nix/lib/module-args.nix.
        { _module.args.fleetLib = fleetLib; }
      ] ++ nixpkgs.lib.optional (secretsFile != null)
        { sops.defaultSopsFile = secretsFile; }
      ++ modules ++ globalModules;

      # One terranixConfiguration per leaf stack (env.stack dot-path).
      # Each emits its own config.tf.json and owns its own state key.
      mkTerranixStack = stackId: terranix.lib.terranixConfiguration {
        inherit system;
        modules = [ (import ./nix/tf {
          inherit stackId fleetLib;
          backend = backend';
          fleetModules = modules;
        }) ] ++ (tfExtraModules.${stackId} or []);
      };

      # tfExtraModules keys are stacks too — a stack may consist solely
      # of raw terranix modules (e.g. a router's uci config) with no
      # fleet-schema entries behind it.
      leafStackIds = nixpkgs.lib.unique
        (nixpkgs.lib.attrNames fleetEval.stacks
         ++ nixpkgs.lib.attrNames tfExtraModules);
      slugOf = id: nixpkgs.lib.replaceStrings [ "." ] [ "-" ] id;

      nixosConfigurations = nixLib.mkNixosConfigurations {
        inherit hosts specialArgs;
        globalModules = baseGlobalModules;
      };
    in
    {
      inherit fleetEval hosts deployable nixosConfigurations;
      # Exposed so a consumer can hand the same helper surface to code that is
      # neither a NixOS module nor a manifest module — flake-level `nix run`
      # utilities, for instance, which are plain imports with explicit args.
      inherit fleetLib;

      fleetManifest = fleetEval.compute;
      fleetAccess   = fleetEval.access;

      colmena = {
        meta = {
          nixpkgs = import nixpkgs {
            localSystem = system;
            overlays = colmenaOverlays;
          };
        } // nixpkgs.lib.optionalAttrs (specialArgs != {}) { inherit specialArgs; }
          // colmenaMeta;
      } // nixLib.mkColmenaNodes {
        hosts = deployable;
        globalModules = baseGlobalModules;
        inherit sopsAgeKeyCommand;
      };

      packages =
        {
          # Flat inventory for non-Nix consumers (the fleet CLI, JSON tools).
          hostsJson = pkgs.writeText "hosts.json"
            (builtins.toJSON fleetEval.hostsJson);

          # Authoritative leaf stack IDs — the fleet CLI reads this to
          # enumerate stacks without re-evaluating the fleet module.
          tf-stack-ids = pkgs.writeText "tf-stack-ids.json"
            (builtins.toJSON leafStackIds);

          # The CLI catalog (ADR-097) — the generated, eval-free projection
          # that REPLACED fleet.toml. Dotted key paths are preserved from
          # the toml era verbatim, so fleet_launcher.config's get()/require()
          # lookups and their call sites needed no changes. Materialized to
          # .cache/fleet/catalog.json by the launcher (auto on first use;
          # refreshed alongside hosts.json by `fleet inventory generate`).
          # Operator-machine paths (age key, sysadmin key) are deliberately
          # absent: those are conventions + FLEET_* env, not fleet facts.
          fleet-catalog = pkgs.writeText "fleet-catalog.json" (builtins.toJSON {
            _meta = { schema = 1; generator = "fleetkit mkFleet (ADR-097)"; };
            fleet = {
              name = fleetEval.settings.name;
              ops_email = fleetEval.settings.opsEmail;
            };
            domains = {
              base = fleetEval.settings.domain.base;
              internal = fleetEval.settings.domain.internal;
              tailnet_suffix = fleetEval.settings.domain.tailnetSuffix;
            };
            backend = backend';
            network = {
              internal_cidr = fleetEval.network.internal_cidr;
              # Historical naming mismatch, resolved here deliberately: the
              # CLI's `lan_cidr` classifies the hypervisor/management net
              # (inventory NIC bucketing) = settings.network.mgmtCidr.
              # settings.network.lanCidr MIRRORS internal_cidr (postgres
              # ACL convenience) and must NOT feed this key.
              lan_cidr = fleetEval.settings.network.mgmtCidr;
            };
            sops.secrets_file = fleetEval.settings.sopsSecretsFile;
            # The file holding integrations.* — provider credentials. The CLI
            # reads the same tree terranix does, so both follow one setting.
            tf.sops_file = fleetEval.settings.tfSopsFile;
            sops.files = fleetEval.settings.sopsFiles;
            # Where the launcher finds PG_CONN_STR for the pg backend.
            backend_pg.conn_str_sops_path = fleetEval.settings.backend.pg.connStrSopsPath;
            # …and the AWS_* keys for the s3 backend. Null = ["integrations"]["aws"].
            backend_s3.creds_sops_path = fleetEval.settings.backend.s3.credsSopsPath;
            cli.extensions_dir = fleetEval.settings.cli.extensionsDir;
            pki.acme_dns_api_base = fleetEval.settings.pki.acmeDnsApiBase;
            pve.install = fleetEval.settings.pveInstall;
            # Provider ENDPOINTS — not credentials. A PVE endpoint is a LAN
            # URL declared in fleet.providers, not a secret, and it is the
            # one thing `fleet pve` needs that the SOPS tree does not carry:
            # a fleet whose credential is an api_token files only that token
            # under integrations.proxmox, so every `pve` verb died on
            # "PROXMOX_VE_ENDPOINT not set" with the endpoint sitting in the
            # manifest two directories away. Credentials still come from
            # SOPS; this only saves the operator from re-typing a fact the
            # fleet already declares.
            providers.proxmox = nixpkgs.lib.mapAttrs (_: p: {
              endpoint = p.endpoint;
              insecure = p.insecure;
            }) (fleetEval.providers.proxmox or { });
            mcp.grafana_token_sops_path = fleetEval.settings.mcp.grafanaTokenSopsPath;
            # Tailnet control plane — the URL is the same one hosts use as
            # --login-server (a public FQDN, not a secret); only the API key
            # is, and that arrives as a SOPS path, never a value. Same shape
            # as providers.proxmox above: the CLI needs a fact the fleet
            # already declares, so it reads it rather than re-deriving it.
            tailnet = {
              control_url = fleetEval.settings.tailnet.controlUrl;
              api_key_sops_path = fleetEval.settings.tailnet.apiKeySopsPath;
            };
            # In-fleet binary caches and the operator key, for
            # `fleet pve build-template`. None of the three is a secret — a
            # cache URL is a LAN address, a trusted-public-key is public by
            # construction, and an SSH PUBLIC key is public by name. Same
            # argument as providers.proxmox above: the template build needs
            # facts the fleet already declares, and without them here the
            # operator hand-edits the image file on the hypervisor, where it
            # drifts from the repo with nothing reporting the drift.
            cache = {
              substituters = fleetEval.settings.cache.substituters;
              trusted_public_keys = fleetEval.settings.cache.trustedPublicKeys;
            };
            network.sysadmin_ssh_key = fleetEval.network.sysadmin_ssh_key;
          });

          # fleet.settings as JSON — read by eval-free CLI features that
          # need Nix-side settings (e.g. `fleet mcp config` deriving the
          # fleet's observability MCP endpoints). Settings hold no secret
          # VALUES (secrets are sops paths), so this is safe to build.
          settings-json = pkgs.writeText "fleet-settings.json"
            (builtins.toJSON fleetEval.settings);

          # The secrets CATALOG — structure and routing only, never values:
          # resource → file path, instances, consumers, env naming, key
          # names. Read by `fleet secrets env-export` and sync tooling so
          # neither needs a Nix eval of its own (nor a decryption key just
          # to know what exists).
          secrets-catalog-json = pkgs.writeText "fleet-secrets-catalog.json"
            (builtins.toJSON (nixpkgs.lib.mapAttrs (_: r:
              (removeAttrs r [ "file" ]) // { file = toString (r.file or null); })
              fleetEval.secrets));

        }
        # One `tf-<env>-<stack>` package per leaf stack:
        # `nix build .#tf-platform-core` → config.tf.json for tofu.
        // nixpkgs.lib.listToAttrs (map (id: {
          name = "tf-${slugOf id}";
          value = mkTerranixStack id;
        }) leafStackIds)
        # Fleet-walking utility commands (service catalog, topology, …).
        // (import ./nix/run {
          inherit pkgs nix-topology nixosConfigurations;
          lib = nixpkgs.lib;
          fleetCompute = fleetEval.compute;
        });
    };

  in
  # Linux-only on purpose (this is an LXC/VM fleet framework) — and
  # eachDefaultSystem would force an x86_64-darwin nixpkgs import, which
  # nixpkgs ≥26.11 turns into a hard eval throw for any consumer that
  # points inputs.nixpkgs at current unstable.
  (flake-utils.lib.eachSystem [ "x86_64-linux" "aarch64-linux" ] (system:
    let
      pkgs = import nixpkgs { inherit system; };
    in {
      packages = rec {
        # The operator CLI (was `sk`; renamed in the extraction).
        # xoa-cli passed explicitly (it's a flake package, not in pkgs) so the
        # `tf adopt` resolvers can import xoa_cli.api.XoRpc (INFRA-274).
        fleet = pkgs.callPackage ./nix/pkgs/_launcher {
          inherit xoa-cli;
          # Bake the images-family template refs so `fleet templates register`
          # reads them eval-free (no runtime `nix eval`, no consumer-flake dep).
          imageTemplatesJson = pkgs.writeText "fleet-image-templates.json"
            (builtins.toJSON (import ./nix/images/deployer/lib {
              inherit nixpkgs nixos-generators;
            }).templatesData);
        };
        default = fleet;

        # pve-cli (wraps Corsinvest cv4pve) — kubectl-style remote CLI for Proxmox VE.
        pve-cli = pkgs.callPackage ./nix/pkgs/pve-cli { };

        # Options documentation site (mdBook + nixosOptionsDoc, generated
        # from the module declarations — cannot drift from the code).
        docs = import ./docs {
          inherit pkgs nixpkgs sops-nix disko;
        };

        # The same option data as structured JSON — the machine/agent-
        # preferred API surface (name, type, default, description,
        # declaring file per option).
        options-json = docs.passthru.optionsJSON;

        # AI-agent discovery surface: fleetkit's whole "how do I use this"
        # manifest in one JSON file — the CLI command tree (help + params),
        # the option surface, and the component interfaces. An agent that
        # imports fleetkit reads this with a single `nix build
        # fleetkit#introspection` (no creds, no SOPS, no running server).
        # Built by running the CLI's own eval-free `fleet describe`, so the
        # commands portion cannot drift from the actual CLI, and options are
        # supplied here (rather than baked into the base `fleet`) to keep the
        # operator wrapper from pulling the docs closure.
        introspection = pkgs.runCommand "fleetkit-introspection.json"
          { nativeBuildInputs = [ fleet ]; }
          ''
            export FLEET_OPTIONS_JSON=${options-json}
            export FLEET_COMPONENTS_DIR=${./nix/components/schema}
            fleet describe > "$out"
          '';

        # xoa-cli — standalone Xen Orchestra operator CLI. Reads over XO
        # REST, mutations over the JSON-RPC websocket. Drives the
        # size_add_gb disk-grow reconciler.
        xoa-cli = pkgs.callPackage ./nix/pkgs/xoa-cli { };

        # Prometheus exporter for XCP-ng tiers via the XO REST API (for

        # Prepared Debian cloud image: firstboot fixes + docker/

      };

      # Two shells only (by design): `default` for local dev, `ci` for
      # non-interactive pipelines. Consumers build their own via lib.mkDevShell.
      devShells = {
        default = import ./nix/shell.nix { inherit pkgs; mode = "dev"; };
        ci      = import ./nix/shell.nix { inherit pkgs; mode = "ci"; };
      };
    }
  )) // {
    lib = {
      inherit mkFleet;
      # Consumer devshell: same toolchain as the framework shell, plus
      # the consumer's fleet (builder discovery) and extras.
      #   fleetkit.lib.mkDevShell { pkgs, fleet, extraPackages, extraShellHook, mode ? "dev" }
      mkDevShell = import ./nix/shell.nix;
      # Lower-level helpers for consumers with bespoke assembly needs.
      inherit (nixLib) mkHosts mkNixosConfigurations mkColmenaNodes;
      # Bootstrap image builders (images component family): mkBootstrapImage(s),
      # the target table, and the ADR-0003 template references. Self-contained
      # (nixpkgs + nixos-generators + modules-as-arguments); never imports the
      # fleet/module eval path.
      images = import ./nix/images/deployer/lib { inherit nixpkgs nixos-generators; };
    };

    # Generic NixOS modules + the fleet schema, importable piecemeal by
    # consumers that don't go through mkFleet.
    nixosModules = {
      default = ./nix/modules;
      fleetSchema = ./nix/fleet;
    };

    templates.minimal = {
      path = ./templates/minimal;
      description = "Minimal fleetkit consumer: one manifest module, one host, mkFleet wiring.";
    };

    # Standalone acceptance: mkFleet over the template's example
    # manifest must assemble a deployable host end-to-end (fleet eval →
    # hostsJson → full NixOS module stack). `nix eval
    # .#checks.x86_64-linux.example-fleet.drvPath` forces it without
    # building; `nix flake check` builds it.
    checks.x86_64-linux =
      (import ./nix/checks.nix {
        inherit nixpkgs mkFleet sops-nix disko;
      })
      # Component interface-schema gates (component-<family>-<name>) — a
      # keyspace disjoint from the checks above (ADR: component model).
      // (import ./nix/components/checks.nix {
        inherit nixpkgs sops-nix disko nixos-generators;
      });
  };
}
