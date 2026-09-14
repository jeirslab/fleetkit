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
  ];

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
