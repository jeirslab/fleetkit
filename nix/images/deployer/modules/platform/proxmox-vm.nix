# Platform layer: Proxmox VE QEMU VM image (vzdump .vma.zst).
#
# The nixos-generators `proxmox` format imports nixpkgs'
# virtualisation/proxmox-image.nix, which builds the disk and the
# qemu-server.conf that `qmrestore` turns back into a VM. This layer
# carries the settings fleetkit learned the hard way; each has its reason.
{ lib, modulesPath, ... }:
{
  # Same module the generator format imports (deduplicated by path), so this
  # layer also works on its own inside a plain nixosSystem.
  imports = [ "${modulesPath}/virtualisation/proxmox-image.nix" ];

  proxmox = {
    qemuConf = {
      name = "nixos-bootstrap";
      cores = 2;
      memory = 2048;
      agent = true;
      # Boot order baked at template creation. Without it `boot:` lands
      # EMPTY in the restored qm config and every clone fails with
      # SeaBIOS "no bootable device".
      boot = "order=virtio0";
    };
    # cloud-init lets PVE inject ipconfig0/hostname at boot — correct static
    # addresses from day one, no DHCP racing.
    cloudInit.enable = true;
  };

  # QEMU guest agent: the PVE API reports guest IPs through it.
  services.qemuGuest.enable = true;

  # proxmox-image.nix installs GRUB to the image device; the guest sees virtio0 as /dev/vda.
  boot.loader.grub.device = lib.mkForce "/dev/vda";

  # Without virtio_blk in the initrd the kernel never sees /dev/vda and
  # stage 1 hangs at "waiting for /dev/vda1". The module a deploy installs
  # is too late for the first boot, so it is baked in.
  boot.initrd.availableKernelModules = [ "virtio_pci" "virtio_blk" "virtio_scsi" "ahci" "sd_mod" ];
  boot.initrd.kernelModules = [ "ext4" ];

  # DHCP fallback on eth0 so a clone whose cloud-init drive has not
  # landed yet is still reachable. tty0 first so /dev/console is ttyS0
  # (PVE wires `serial0: socket` by default; a boot failure is never a black box).
  systemd.network.networks."10-eth0" = {
    matchConfig.Name = "eth0";
    networkConfig.DHCP = "ipv4";
    dhcpV4Config = { UseDNS = true; UseHostname = false; };
  };
  boot.kernelParams = [ "console=tty0" "console=ttyS0,115200" ];
  systemd.services."serial-getty@ttyS0".enable = true;

  system.nixos.tags = [ "proxmox-vm" ];
}
