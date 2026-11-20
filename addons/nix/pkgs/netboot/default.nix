# fleetkit.addons.netboot: network-boot one NixOS image (a system that imports nixpkgs'
# installer/netboot/netboot-minimal.nix, or netboot.nix) to any machine on this host's LAN that
# PXE-boots, through pixiecore in proxy-DHCP mode: the LAN's own DHCP server keeps handing out
# addresses and is not changed; pixiecore only answers the PXE part (BIOS and UEFI, chaining iPXE).
# Off by default. With `macs` empty every PXE client on the LAN gets the image while it is on;
# with `macs` set, only those machines do (pixiecore's API mode against a static answer per MAC,
# served on localhost; any other MAC gets no answer and boots as it would without pixiecore).
{
  config,
  lib,
  pkgs,
  ...
}:
let
  inherit (lib) mkOption mkEnableOption types mkIf;
  cfg = config.fleetkit.addons.netboot;
  image = cfg.system.config;
  build = image.system.build;
  kernel = "${build.kernel}/${image.system.boot.loader.kernelFile}";
  initrd = "${build.netbootRamdisk}/initrd";
  cmdLine = "init=${build.toplevel}/init ${toString image.boot.kernelParams}";
  apiPort = cfg.port + 1;
  # pixiecore asks GET <api>/v1/boot/<mac> (lower case, colons); a file per allowed MAC answers.
  apiRoot = pkgs.runCommand "netboot-api" { } (
    ''
      mkdir -p $out/v1/boot
      ln -s ${kernel} $out/kernel
      ln -s ${initrd} $out/initrd
    ''
    + lib.concatMapStrings (mac: ''
      cat > "$out/v1/boot/${lib.toLower mac}" <<'EOF'
      ${builtins.toJSON {
        kernel = "http://127.0.0.1:${toString apiPort}/kernel";
        initrd = [ "http://127.0.0.1:${toString apiPort}/initrd" ];
        cmdline = cmdLine;
      }}
      EOF
    '') cfg.macs
  );
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

    macs = mkOption {
      type = types.listOf (types.strMatching "([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}");
      default = [ ];
      example = [ "d8:cb:8a:a1:13:45" ];
      description = "Only these MAC addresses get the image. Empty: every PXE client on the LAN.";
    };

    port = mkOption {
      type = types.port;
      default = 4280;
      description = ''
        pixiecore's HTTP port for the kernel and initrd (also its status page). Not 80, which a
        host often serves already. With `macs`, the per-MAC answers are on port + 1, localhost only.
      '';
    };

    openFirewall = mkOption {
      type = types.bool;
      default = true;
      description = "Open DHCP-proxy (UDP 67, 4011), TFTP (UDP 69) and `port` on the firewall.";
    };
  };

  config = mkIf cfg.enable (lib.mkMerge [ {
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
      mode = if cfg.macs == [ ] then "boot" else "api";
      inherit kernel initrd cmdLine;
      apiServer = "http://127.0.0.1:${toString apiPort}";
      # Do not bind the DHCP port to an interface: answers go out as raw packets, which also works
      # in a container.
      dhcpNoBind = true;
    };
  }
  (mkIf (cfg.macs != [ ]) {
    systemd.services.netboot-api = {
      description = "Per-MAC netboot answers for pixiecore";
      wantedBy = [ "multi-user.target" ];
      before = [ "pixiecore.service" ];
      serviceConfig = {
        ExecStart = "${lib.getExe pkgs.darkhttpd} ${apiRoot} --addr 127.0.0.1 --port ${toString apiPort}";
        DynamicUser = true;
        Restart = "always";
      };
    };
  })
  ]);
}
