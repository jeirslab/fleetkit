# Platform layer: Proxmox VE LXC container template.
#
# The nixos-generators `proxmox-lxc` format imports nixpkgs'
# virtualisation/proxmox-lxc.nix (make-system-tarball + /sbin/init). This
# layer only adds what an unprivileged NixOS CT on PVE ≥ 9 needs.
#
# No network unit is baked here on purpose: PVE's NixOS setup plugin
# (ostype=nixos) writes /etc/systemd/network/eth0.network from the CT's
# net0 ip=/gw= at create time, and manageNetwork=false lets networkd
# consume it. A baked DHCP unit would sort before it and shadow the
# declared address (fleetkit INFRA-86).
{ lib, modulesPath, ... }:
{
  # Same module the generator format imports (deduplicated by path), so this
  # layer also works on its own inside a plain nixosSystem.
  imports = [ "${modulesPath}/virtualisation/proxmox-lxc.nix" ];

  proxmoxLXC = {
    enable = true;
    manageNetwork = false;
    manageHostName = false;   # /etc/hostname comes from `pct create --hostname`
    privileged = false;
  };

  # Units that cannot succeed in an unprivileged container.
  systemd.suppressedSystemUnits = [
    "dev-mqueue.mount"
    "sys-kernel-debug.mount"
    "sys-fs-fuse-connections.mount"
    "systemd-sysctl.service"
  ];

  system.nixos.tags = [ "proxmox-lxc" ];
}
