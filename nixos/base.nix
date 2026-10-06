# The lab's part of a guest's NixOS system: what follows from the model, so a
# tenant's module only has to say what runs in the guest.
#
#   platform   lxc -> nixpkgs' proxmox-lxc profile, vm -> the qemu guest profile
#   names      the guest's name is its host name
#   operators  sshd with keys only; one account per role granted on this guest
#              (fleet.operators grants), holding the keys of the granted
#              principals; `sudo` roles are in wheel; root accepts the keys of
#              principals who may sudo or deploy
#   fleet.*    read-only facts for tenant modules: fleet.compute.<guest>.internal_ip,
#              fleet.settings.adminSshKeys, fleet.settings.domain.internal
#
# `deployment` is declared so modules can state deploy preferences
# (buildOnTarget); the deploy layer reads it, nothing here acts on it.
#
# Only what every guest needs to be reachable and managed. Monitoring agents,
# caches, mail and the like are the estate's own modules.
{
  guest,
  name,
  compute,
  accounts,
  rootKeys,
  internalDomain,
}:
{ lib, modulesPath, ... }:
let
  isLxc = guest.kind == "lxc";
  loose = lib.mkOption {
    type = lib.types.attrsOf lib.types.anything;
    default = { };
  };
in
{
  imports = [
    (modulesPath + (if isLxc then "/virtualisation/proxmox-lxc.nix" else "/profiles/qemu-guest.nix"))
  ];

  options = {
    deployment = loose;
    fleet = loose;
  }
  # Tenant modules shared by both kinds set proxmoxLXC.*; on a vm it is inert.
  // lib.optionalAttrs (!isLxc) { proxmoxLXC = loose; };

  config = lib.mkMerge [
    {
      networking.hostName = lib.mkDefault name;
      nixpkgs.hostPlatform = lib.mkDefault "x86_64-linux";
      fleet = {
        inherit compute;
        settings = {
          adminSshKeys = rootKeys;
          domain.internal = internalDomain;
        };
      };
    }
    {
      services.openssh = {
        enable = true;
        settings.PasswordAuthentication = lib.mkDefault false;
        settings.KbdInteractiveAuthentication = lib.mkDefault false;
      };
      users.users = {
        root.openssh.authorizedKeys.keys = rootKeys;
      }
      // lib.mapAttrs (_: a: {
        isNormalUser = lib.mkDefault true;
        extraGroups = lib.optional a.sudo "wheel";
        openssh.authorizedKeys.keys = a.keys;
      }) accounts;
      # The accounts have keys and no passwords.
      security.sudo.wheelNeedsPassword = lib.mkDefault false;
    }
    (lib.mkIf isLxc { proxmoxLXC.manageHostName = true; })
    (lib.mkIf (!isLxc) {
      fileSystems."/" = lib.mkDefault {
        device = "/dev/vda1";
        fsType = "ext4";
      };
      boot.loader.grub.device = lib.mkDefault "/dev/vda";
    })
  ];
}
