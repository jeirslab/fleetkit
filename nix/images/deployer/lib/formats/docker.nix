# Custom nixos-generators format: Docker/OCI system tarball.
#
# nixos-generators' bundled `docker` format still sets
# `services.journald.console`, which current nixpkgs removed (hard eval
# assertion). This is the same format with the current option names:
# nixpkgs' docker-image.nix (make-system-tarball via the docker-container
# profile, /init boots systemd), no bootloader, journal to the console.
# Registered through `customFormats` in lib/default.nix, so it shadows the
# bundled one under the same name.
{ modulesPath, lib, ... }:
{
  imports = [ "${modulesPath}/virtualisation/docker-image.nix" ];

  boot.loader.grub.enable = lib.mkForce false;
  boot.loader.systemd-boot.enable = lib.mkForce false;
  services.journald.settings.Journal = {
    ForwardToConsole = true;
    TTYPath = "/dev/console";
  };

  formatAttr = "tarball";
  fileExtension = ".tar.xz";
}
