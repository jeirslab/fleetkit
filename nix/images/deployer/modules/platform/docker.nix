# Platform layer: the same golden template as a Docker/OCI root filesystem.
#
# The nixos-generators `docker` format imports nixpkgs'
# virtualisation/docker-image.nix: a system tarball whose /init boots
# systemd inside the container. Register it with
#   docker import --change 'ENTRYPOINT ["/init"]' result/tarball/*.tar.xz <tag>
# (what `deployer templates register docker` does) and run it with
# `docker run --privileged`. Useful as a CI stand-in for the VM/LXC
# variants and as the base of the deployer's own image (ADR-0001 D3).
{ lib, modulesPath, ... }:
{
  # Same module the generator format imports (deduplicated by path).
  imports = [ "${modulesPath}/virtualisation/docker-image.nix" ];

  # The container runtime owns networking and the firewall (iptables does
  # not work inside Docker; docker-image.nix disables it already).
  networking.useDHCP = false;

  # No kernel, no bootloader, no initrd: the image is a rootfs.
  boot.loader.grub.enable = lib.mkForce false;

  system.nixos.tags = [ "docker" ];
}
