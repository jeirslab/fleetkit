# Platform layer: Xen Orchestra / XCP-ng VM (UEFI HVM, raw disk).
#
# The nixos-generators `raw-efi` format produces a single GPT raw disk
# (ESP + ext4 root, GRUB EFI removable, growPartition on boot). Uploaded
# as a VDI and wrapped in a template, the terraform `xenorchestra_vm`
# resource clones it with `hvm_boot_firmware = "uefi"`.
#
# UEFI on purpose: BIOS HVM + GRUB hung silently in early init on Xen in
# fleetkit's experiments (v3/v4); UEFI is the path that boots.
{ lib, pkgs, ... }:
{
  # 8 GiB base disk; the root partition grows to the VDI size at first boot.
  virtualisation.diskSize = lib.mkDefault (8 * 1024);

  boot.initrd.availableKernelModules = [
    # Xen PV drivers under HVM (xen-platform-pci is built in, not a module).
    "xen_blkfront" "xen_netfront"
    # QEMU fallbacks so the same disk also boots under plain KVM for tests.
    "virtio_pci" "virtio_blk" "virtio_scsi" "ahci" "sd_mod"
  ];

  # Guest agents so XO reports the address back through its API (which is
  # how inventory discovery finds a fresh VM).
  services.xe-guest-utilities.enable = true;
  services.qemuGuest.enable = true;

  # DHCP on the first NIC via networkd (there is no cloud-config drive on
  # NixOS XO VMs). The client identifier is the MAC, so a VM recreated with
  # the same MAC keeps its lease.
  networking.useDHCP = false;
  networking.useNetworkd = true;
  systemd.network.networks."10-wan" = {
    matchConfig.Name = [ "eth*" "en*" ];
    networkConfig.DHCP = "ipv4";
    dhcpV4Config = {
      UseHostname = false;
      SendHostname = true;
      ClientIdentifier = "mac";
    };
  };

  # Fan out the console: tty0 / hvc0 (Xen virtual console) / ttyS0, so at
  # least one of the XO console views shows boot progress.
  boot.kernelParams = [ "console=tty0" "console=hvc0" "console=ttyS0,115200" ];
  systemd.services."serial-getty@hvc0".enable = true;
  systemd.services."serial-getty@ttyS0".enable = true;

  # LTS kernel: the latest kernel carried a xen-blkfront regression on
  # XCP-ng (disk + net go silent ~2 min after first boot; fleetkit pins the
  # same for post-install systems). mkDefault so a consumer can move on.
  boot.kernelPackages = lib.mkDefault pkgs.linuxPackages_6_12;

  system.nixos.tags = [ "xen-orchestra" ];
}
