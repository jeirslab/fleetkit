{ config, lib, pkgs, stackId ? null, fleetLib ? null, ... }:

# LXC container + KVM VM emitter for a single leaf stack.

let
  helpers = import ../../lib/tf/proxmox.nix { inherit config lib pkgs fleetLib; };

  computeInStack = lib.filterAttrs
    (_: c: (c.enabled or true)
           && "${c.env}.${c.stack}" == stackId
           && lib.hasPrefix "proxmox." c.provider_instance)
    config.fleet.compute;

  containers = lib.filterAttrs (_: c: c.kind == "container") computeInStack;
  vms        = lib.filterAttrs (_: c: c.kind == "vm") computeInStack;

  emitContainers = lib.mapAttrs helpers.mkContainer containers;
  emitVms        = lib.mapAttrs helpers.mkVm vms;

  # Containers carrying raw lxc.conf lines get a terraform_data companion.
  extraConf = lib.filterAttrs (_: c: (c.lxc_extra_conf or []) != []) containers;
  emitExtraConf = lib.mapAttrs' (n: c: lib.nameValuePair "${n}-lxc-conf" (helpers.mkLxcExtraConf n c)) extraConf;

in {
  config = lib.mkIf (stackId != null && computeInStack != {}) (
    lib.mkMerge [
      (lib.mkIf (containers != {}) {
        resource.proxmox_virtual_environment_container = emitContainers;
      })
      (lib.mkIf (vms != {}) {
        resource.proxmox_virtual_environment_vm = emitVms;
      })
      (lib.mkIf (extraConf != {}) {
        resource.terraform_data = emitExtraConf;
      })
    ]
  );
}
