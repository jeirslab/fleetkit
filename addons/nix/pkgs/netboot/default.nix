# fleetkit.addons.netboot: network-boot one NixOS image (a system that imports nixpkgs'
# installer/netboot/netboot-minimal.nix, or netboot.nix) to any machine on this host's LAN that
# PXE-boots, through pixiecore in proxy-DHCP mode: the LAN's own DHCP server keeps handing out
# addresses and is not changed; pixiecore only answers the PXE part (BIOS and UEFI, chaining iPXE).
# Off by default. Every PXE client on the LAN gets the image while it is on, so serve an image
# that does nothing by itself (an installer with sshd), and switch it off after the install.
{
  config,
  lib,
  ...
}:
let
  inherit (lib) mkOption mkEnableOption types mkIf;
  cfg = config.fleetkit.addons.netboot;
  image = cfg.system.config;
  build = image.system.build;
in
{
  options.fleetkit.addons.netboot = {
    enable = mkEnableOption "network boot of `system` for the LAN (pixiecore, proxy DHCP)";

    system = mkOption {
      type = types.nullOr types.raw;
      default = null;
      example = lib.literalExpression ''
        nixpkgs.lib.nixosSystem { modules = [ "''${modulesPath}/installer/netboot/netboot-minimal.nix" ./installer.nix ]; }
      '';
      description = "The NixOS system to serve (the result of nixosSystem), built with the netboot module.";
    };

    port = mkOption {
      type = types.port;
      default = 4280;
      description = ''
        pixiecore's HTTP port for the kernel and initrd (also its status page). Not 80, which a
        host often serves already.
      '';
    };

    openFirewall = mkOption {
      type = types.bool;
      default = true;
      description = "Open DHCP-proxy (UDP 67, 4011), TFTP (UDP 69) and `port` on the firewall.";
    };
  };

  config = mkIf cfg.enable {
    assertions = [
      {
        assertion = cfg.system != null && build ? netbootRamdisk;
        message = "fleetkit.addons.netboot.system must be a nixosSystem that imports installer/netboot/netboot.nix.";
      }
    ];

    services.pixiecore = {
      enable = true;
      inherit (cfg) openFirewall port;
      statusPort = cfg.port;
      mode = "boot";
      kernel = "${build.kernel}/${image.system.boot.loader.kernelFile}";
      initrd = "${build.netbootRamdisk}/initrd";
      cmdLine = "init=${build.toplevel}/init ${toString image.boot.kernelParams}";
      # Do not bind the DHCP port to an interface: answers go out as raw packets, which also works
      # in a container.
      dhcpNoBind = true;
    };
  };
}
