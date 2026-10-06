# The NixOS module the fixture's machine node names. A root file system and a
# boot loader are set here because the base supplies neither for a machine.
_: {
  fileSystems."/" = {
    device = "/dev/disk/by-label/root";
    fsType = "ext4";
  };
  boot.loader.grub.enable = false;
  services.openssh.enable = true;
  system.stateVersion = "25.05";
}
