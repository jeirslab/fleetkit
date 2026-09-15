{ config, lib, ... }:

# Baremetal platform — a physical machine (or any host provisioned outside a
# supported hypervisor: an externally-managed VM, a laptop, a NUC). It carries
# its own hardware-configuration.nix, bootloader, and network manager, so the
# framework's hypervisor-guest assumptions do NOT apply here.
#
# Two things have to be actively CANCELLED, both for the same reason the
# ../pve/qemu.nix and ../xcpng/vm.nix branches cancel them: the modules that
# set them are imported unconditionally (Nix can't gate `imports` on config),
# so a non-matching platform has to undo their effect rather than opt out.
#
#   1. proxmox-lxc profile (imported by ../pve/lxc.nix) — defaults
#      boot.isContainer = true, which suppresses the bootloader. A baremetal
#      host must boot its own kernel, so force it off.
#   2. systemd-networkd (../core sets a DHCP eth0; ../fleet-member.nix mkForces
#      a fleet-aware eth0). Both assume a single hypervisor NIC named eth0 on a
#      fleet bridge. A baremetal host brings its own stack — NetworkManager,
#      wifi, bonding, whatever — so disable networkd fleet-side and let the host
#      config own networking. The stale network definitions become inert once
#      the daemon is off.
#
# Everything substrate-specific beyond this (systemd-boot vs GRUB, filesystems,
# NIC naming) belongs to the host's own modules, never the framework.

{
  config = lib.mkIf (config.infra.platform.type == "baremetal") {
    proxmoxLXC.enable = lib.mkForce false;

    networking.useNetworkd = lib.mkForce false;
    systemd.network.enable = lib.mkForce false;
  };
}
