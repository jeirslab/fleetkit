# lib/ — the pure API of fleetkit-deployer.
#
# Everything here is a function of its arguments; nothing reads the
# environment. The flake re-exports this attrset as `lib`, and the CLI
# reaches it through `nix build --expr` (see pkgs/cli/deployer_cli/nix.py).
{ nixpkgs, nixos-generators }:

let
  lib = nixpkgs.lib;

  # ── The target table ───────────────────────────────────────────
  #
  # One entry per platform the deployer can bootstrap. `format` is the
  # nixos-generators format that produces the artifact, `platform` the
  # thin NixOS layer that adapts the golden template to that platform,
  # `artifact` the glob (relative to the build result) of the file the
  # registration step uploads, `register` the platform verb of
  # `deployer templates register`.
  targets = {
    proxmox-lxc = {
      description = "Proxmox VE LXC container template (.tar.xz, pct/vztmpl)";
      format = "proxmox-lxc";
      platform = ../modules/platform/proxmox-lxc.nix;
      artifact = "tarball/*.tar.xz";
      register = "proxmox-lxc";
    };
    proxmox-vm = {
      description = "Proxmox VE QEMU VM image, cloud-init OFF (vzdump .vma.zst, qmrestore → qm template). DHCP + baked deploy key — the bare bootstrap for a self-configuring guest (e.g. an ML box). Upstream proxmoxVMA/proxmoxImage.";
      format = "proxmox";
      platform = ../modules/platform/proxmox-vm.nix;
      artifact = "*.vma.zst";
      register = "proxmox-vm";
    };
    proxmox-vm-cloud = {
      description = "Proxmox VE cloud-init VM disk (raw .img; register as a template, clone via a cloud-init drive). PVE injects per-clone IP/hostname/keys. Upstream proxmoxCloudImage.";
      format = "proxmox-cloud";
      platform = ../modules/platform/proxmox-vm-cloud.nix;
      artifact = "*.img";
      # NOTE: deploy-side registration for a raw disk differs from the VMA
      # (qm importdisk + qm template, not qmrestore); the register verb is a
      # deploy-side TODO. Kept as proxmox-vm here so the table stays complete.
      register = "proxmox-vm";
    };
    xen-orchestra = {
      description = "Xen Orchestra / XCP-ng UEFI VM disk (raw .img, imported as a VDI → template)";
      format = "raw-efi";
      platform = ../modules/platform/xen-orchestra.nix;
      artifact = "*.img";
      register = "xen-orchestra";
    };
    docker = {
      description = "Docker/OCI root filesystem of the same template (.tar.xz, docker import)";
      format = "docker";
      platform = ../modules/platform/docker.nix;
      artifact = "tarball/*.tar.xz";
      register = "docker";
    };
  };

  # The same table without the module paths: JSON-serialisable, exported
  # as the flake's `targets` output for `nix eval --json`.
  targetsData = lib.mapAttrs (_: t: removeAttrs t [ "platform" ]) targets;

  # ── Template references ─────────────────────────────────────────
  #
  # The STABLE public identity of each template as it lands on a platform,
  # deliberately decoupled from how it is BUILT (`targets`) and how it is
  # REGISTERED (the CLI). Two consumers read one object and must agree:
  #   * the deployer's `templates register` verbs, to know the name / VMID /
  #     ostype / file to create on the platform;
  #   * fleetkit and a fleet manifest, to know what to clone from.
  # Because both sides read the same reference, the build or registration
  # process can change and fleetkit does not — the reference IS the contract.
  #
  # NAMING (the contract other projects reference):
  #   * `name` identifies the template. Override it at build/register time to
  #     generate VARIATIONS from the same machinery (e.g. a GPU LXC) — the name
  #     is not fixed, only defaulted here.
  #   * the DEFAULT reference is the `latest` handle: consumers reference
  #     `<name>-latest` and always get the newest build.
  #   * VERSIONING is opt-in (`--version` / a module option): a pinned version
  #     publishes `<name>-v<N>` AND (re)publishes `<name>-latest`, so `latest`
  #     always exists and a specific build stays pinnable. Versions are a
  #     registration concern, not baked into this default reference.
  templates = {
    proxmox-lxc = rec {
      platform = "proxmox";
      kind = "lxc";
      fromTarget = "proxmox-lxc";
      name = "nixos-bootstrap-lxc";
      ext = ".tar.xz"; # pct requires the suffix
      latest = "${name}-latest${ext}"; # <storage>:vztmpl/<latest>
      ostype = "nixos";
      unprivileged = true;
    };
    proxmox-vm = rec {
      platform = "proxmox";
      kind = "vm";
      fromTarget = "proxmox-vm";
      name = "nixos-bootstrap-vm";
      latest = "${name}-latest"; # VM template name_label
      vmid = 9000; # fleetkit mkVm's default clone source (cloud-init OFF)
      firmware = "seabios";
      # cloud-init OFF: the bare bootstrap (DHCP + deploy key). A fleet VM
      # cloning this MUST set cloud_init.enable = false so mkVm drops the
      # initialization block; the cloud-init template below (9001) is the
      # one to clone when per-instance injection is wanted.
      cloudInit = false;
    };
    proxmox-vm-cloud = rec {
      platform = "proxmox";
      kind = "vm";
      fromTarget = "proxmox-vm-cloud";
      name = "nixos-bootstrap-cloud";
      latest = "${name}-latest";
      vmid = 9001;
      firmware = "seabios";
      cloudInit = true;
    };
    xen-orchestra = rec {
      platform = "xen-orchestra";
      kind = "vm";
      fromTarget = "xen-orchestra";
      name = "nixos-bootstrap-xcpng";
      latest = "${name}-latest"; # XO template name_label
      firmware = "uefi";
    };
    docker = rec {
      platform = "docker";
      kind = "container";
      fromTarget = "docker";
      name = "nixos-bootstrap";
      latest = "${name}:latest"; # image tag
    };
  };

  # Already plain data (strings/ints/bools) — exported as the flake's
  # `templates` output for `nix eval --json .#templates`.
  templatesData = templates;

  # Modules that make up one bootstrap system for a target.
  bootstrapModules =
    { target
    , deployKey
    , extraAuthorizedKeys ? [ ]
    , hostName ? null
    , substituters ? [ ]
    , trustedPublicKeys ? [ ]
    , extraModules ? [ ]
    }:
    let t = targets.${target} or (throw "fleetkit-deployer: unknown target '${target}' (known: ${lib.concatStringsSep ", " (lib.attrNames targets)})");
    in [
      ../modules/bootstrap.nix
      t.platform
      {
        deployer.bootstrap = {
          inherit deployKey extraAuthorizedKeys substituters trustedPublicKeys;
        } // lib.optionalAttrs (hostName != null) { inherit hostName; };
      }
    ] ++ extraModules;

  # Formats we carry ourselves (see each file's header for why). A custom
  # format with the same name as a bundled one shadows it.
  customFormats = {
    docker = ./formats/docker.nix;
    proxmox-cloud = ./formats/proxmox-cloud.nix;
  };

  # One image derivation. The nixos-generators format module is the only
  # place the generator is touched, so swapping the engine (e.g. for
  # nixpkgs' upstream `system.build.images`) is a one-function change.
  mkBootstrapImage =
    { system ? "x86_64-linux", target, ... }@args:
    nixos-generators.nixosGenerate {
      inherit system customFormats;
      format = targets.${target}.format;
      modules = bootstrapModules (removeAttrs args [ "system" ]);
    };

  # All targets at once, same key: { proxmox-lxc = <drv>; proxmox-vm = …; … }
  mkBootstrapImages =
    { system ? "x86_64-linux", ... }@args:
    lib.mapAttrs (name: _: mkBootstrapImage (args // { target = name; })) targets;

  # A plain nixosSystem (no image format) — for tests and for consumers
  # that want the golden config inside their own assembly.
  mkBootstrapSystem =
    { system ? "x86_64-linux", target ? "proxmox-lxc", ... }@args:
    nixpkgs.lib.nixosSystem {
      inherit system;
      modules = bootstrapModules ((removeAttrs args [ "system" ]) // { inherit target; });
    };
in
{
  inherit targets targetsData templates templatesData bootstrapModules mkBootstrapImage mkBootstrapImages mkBootstrapSystem;
}
