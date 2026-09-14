{ lib
, python3
, sops
, opentofu
, age
  # Standalone XO library+CLI (flake package). Imported by the `tf adopt`
  # resolvers for JSON-RPC lookups (xoa_cli.api.XoRpc — cloud-config UUIDs,
  # INFRA-274); the `fleet xoa` shim still soft-imports its click group.
, xoa-cli
  # The framework ansible tree (playbooks + roles) that ships with
  # fleetkit. Baked into the wrapper as $FLEET_ANSIBLE_DIR so
  # `fleet ansible run` and the env bootstrap can resolve framework
  # playbooks/roles when running from the installed package (outside a
  # fleetkit checkout).
, ansibleTree ? ../../../ansible
  # The framework nix/modules/ tree, baked in as $FLEET_MODULES_DIR so
  # module-adjacent playbooks (nix/modules/**/<name>.yml, e.g.
  # infra/build/attic/attic-rebootstrap.yml) resolve from the
  # installed package too.
, modulesTree ? ../../modules
  # The framework nix/images/ tree, baked in as $FLEET_IMAGES_DIR so
  # `fleet pve build-template` can SHIP the canonical image definition to
  # the hypervisor instead of requiring it be hand-copied there. A copy
  # sitting at /root/builder/ on a PVE node drifts silently from the repo,
  # and nothing reports the drift — the build just keeps succeeding against
  # a stale file.
, imagesTree ? ../../images
  # The images-family template references (lib.images.templatesData) as a JSON
  # file, baked in as $FLEET_IMAGE_TEMPLATES so `fleet templates register`
  # reads the contract eval-free (fleetkit convention: Nix data reaches the CLI
  # via built artifacts, never `nix eval` at runtime).
, imageTemplatesJson ? null
  # The committed component interface schemas (nix/components/schema), baked as
  # $FLEET_COMPONENTS_DIR so `fleet describe` can surface component interfaces
  # to an AI agent eval-free. Lightweight (committed JSON), unlike the option
  # surface (FLEET_OPTIONS_JSON) which the `introspection` package supplies so
  # the base wrapper need not pull the docs closure.
, componentsDir ? ../../components/schema
}:

python3.pkgs.buildPythonApplication {
  pname = "fleet-launcher";
  version = "0.3.0";
  pyproject = true;

  src = ./.;

  build-system = with python3.pkgs; [
    setuptools
  ];

  dependencies = (with python3.pkgs; [
    click
    rich
    pyyaml
    proxmoxer
    requests
    # images component (folded from fleetkit-deployer): Proxmox template
    # registration (paramiko) + the Xen Orchestra JSON-RPC path
    # (websocket-client). Lazy-imported by the back-ends.
    paramiko
    websocket-client
  ]) ++ [
    xoa-cli  # importable: xoa_cli.api.XoRpc for adopt JSON-RPC lookups
  ];

  # Runtime-dep check dropped: optional integrations import lazily. Drop
  # the runtime-deps check so the build doesn't fail; the import only
  # fires if a user actually runs that subcommand.
  dontCheckRuntimeDeps = true;

  # Expose sops, opentofu, and age on PATH so `fleet` works outside nix develop.
  makeWrapperArgs = [
    "--prefix PATH : ${lib.makeBinPath [ sops opentofu age ]}"
    "--set-default FLEET_ANSIBLE_DIR ${ansibleTree}"
    "--set-default FLEET_MODULES_DIR ${modulesTree}"
    "--set-default FLEET_IMAGES_DIR ${imagesTree}"
  ] ++ lib.optional (imageTemplatesJson != null)
    "--set-default FLEET_IMAGE_TEMPLATES ${imageTemplatesJson}"
  ++ lib.optional (componentsDir != null)
    "--set-default FLEET_COMPONENTS_DIR ${componentsDir}";

  # Unit + integration tests (CLI composition, --dump-verbs introspection,
  # fleet smoke) run in the sandbox via pytest.
  nativeCheckInputs = with python3.pkgs; [ pytestCheckHook ];

  pythonImportsCheck = [ "fleet_launcher" ];

  meta = {
    description = "fleet — unified TUI/CLI launcher for fleetkit-managed infrastructure";
    license = lib.licenses.mit;
    mainProgram = "fleet";
  };
}
