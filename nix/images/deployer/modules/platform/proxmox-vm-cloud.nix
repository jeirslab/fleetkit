# Platform layer: Proxmox VE QEMU VM image WITH cloud-init.
#
# The `proxmox-vm-cloud` target. Identical to the bare `proxmox-vm` layer
# (proxmox-vm.nix) except cloud-init is ON: PVE injects ipconfig0 / hostname
# / SSH keys through the cloud-init drive at clone time, so a clone comes up
# on its declared static address with no DHCP racing. This is the template
# for guests that want per-instance configuration — e.g. a personal NixOS
# VM cloned to a fixed IP. fleetkit's `mkVm` emits the matching
# `initialization` block whenever `cloud_init.enable` is true (the default).
#
# The DHCP unit baked by proxmox-vm.nix stays as the fallback until the
# cloud-init drive lands.
{ lib, ... }:
{
  imports = [ ./proxmox-vm.nix ];

  # Override the base layer's cloud-init-off default.
  proxmox.cloudInit.enable = lib.mkForce true;

  system.nixos.tags = [ "proxmox-vm-cloud" ];
}
