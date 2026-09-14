# modules/bootstrap.nix — the golden bootstrap template (ADR-0002).
#
# Purpose: boot, get an address, accept the deploy key over SSH. That is
# the whole contract. Users, services, secrets, monitoring — all of it is
# pushed afterwards by colmena / the deployer pipeline, so this file must
# stay as small as it can be: every package here is in every image on
# every platform, forever.
#
# Two kinds of settings live here:
#   * `deployer.bootstrap.*` — the parameters an image is built with. The
#     deploy key is the one required value (a template nobody can log
#     into is useless, so the module refuses to evaluate without it).
#   * the minimisation knobs below `config` — what is switched OFF to keep
#     the closure small. Each one is `mkDefault` so a consumer can turn a
#     feature back on from its own module without fighting this one.
{ config, lib, pkgs, ... }:

let
  inherit (lib) mkOption mkDefault mkForce types;
  cfg = config.deployer.bootstrap;

  # Refuse anything that is not a public key. Baking a private key into a
  # template that is then cloned across an estate is unrecoverable, and
  # the mistake is one wrong `cat` away — so it is checked at eval time,
  # not left to a reviewer.
  publicKeyPrefixes = [ "ssh-ed25519 " "ssh-rsa " "ecdsa-sha2-" "sk-ssh-ed25519@" "sk-ecdsa-sha2-" ];
  checkPublicKey = what: key:
    if lib.hasInfix "PRIVATE KEY" key then
      throw "deployer.bootstrap.${what}: this looks like a PRIVATE key; pass the public half (the .pub file)"
    else if !(lib.any (p: lib.hasPrefix p key) publicKeyPrefixes) then
      throw "deployer.bootstrap.${what}: not an OpenSSH public key (expected one of: ${lib.concatStringsSep ", " publicKeyPrefixes})"
    else key;
in
{
  options.deployer.bootstrap = {
    deployKey = mkOption {
      type = types.str;
      # No default: required by design (see header).
      example = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIExampleExampleExampleExampleExampleExampl deploy@example.com";
      description = ''
        OpenSSH public key granted root access on the fresh guest. This is
        the ONLY credential in the image: the first colmena push (or the
        deployer pipeline) authenticates with it and installs everything
        else. Private keys are rejected at evaluation time.
      '';
    };

    extraAuthorizedKeys = mkOption {
      type = types.listOf types.str;
      default = [ ];
      example = [ "ssh-ed25519 AAAA… operator@example.com" ];
      description = ''
        Additional public keys for root, e.g. an operator's break-glass key.
        Same checks as `deployKey`.
      '';
    };

    hostName = mkOption {
      type = types.str;
      default = "nixos-bootstrap";
      description = ''
        Host name baked into the template. Platform layers that receive the
        name from the hypervisor (Proxmox LXC, cloud-init) override this.
      '';
    };

    substituters = mkOption {
      type = types.listOf types.str;
      default = [ ];
      example = [ "https://cache.example.com" ];
      description = ''
        Binary caches trusted from the first boot. The first deploy of a
        fresh guest substitutes its closure from here instead of building
        it on a small guest — the module a deploy installs is too late for
        the deploy that installs it. Empty leaves nix defaults untouched.
      '';
    };

    trustedPublicKeys = mkOption {
      type = types.listOf types.str;
      default = [ ];
      example = [ "cache.example.com-1:AAAA…=" ];
      description = "Signing keys of `substituters`, in the same order.";
    };

    stateVersion = mkOption {
      type = types.str;
      default = "25.11";
      description = "`system.stateVersion` of the template. Bump deliberately, with the nixpkgs pin.";
    };
  };

  config = {
    # ── The contract: root + the deploy key, over hardened SSH ─────────
    users.mutableUsers = false;
    users.users.root = {
      shell = pkgs.bash;
      openssh.authorizedKeys.keys =
        [ (checkPublicKey "deployKey" cfg.deployKey) ]
        ++ map (checkPublicKey "extraAuthorizedKeys") cfg.extraAuthorizedKeys;
    };

    services.openssh = {
      enable = true;
      settings = {
        PermitRootLogin = "prohibit-password";
        PasswordAuthentication = false;
        KbdInteractiveAuthentication = false;
      };
    };

    networking.hostName = mkDefault cfg.hostName;

    # ── What the first deploy needs to find ────────────────────────────
    nix.settings = {
      experimental-features = [ "nix-command" "flakes" ];
      extra-substituters = cfg.substituters;
      extra-trusted-public-keys = cfg.trustedPublicKeys;
    };
    # sops-nix age key landing zone: colmena uploads the key here before
    # activation, so the directory must exist with the right mode already.
    systemd.tmpfiles.rules = [ "d /var/lib/sops-nix 0700 root root -" ];

    # dbus-broker from the first boot. NixOS's `switchInhibitors` check
    # refuses a live activation that CHANGES the dbus implementation; a
    # template shipping classic dbus makes every fresh guest's first deploy
    # fail until retried with --reboot (found the hard way in fleetkit).
    services.dbus.implementation = mkDefault "broker";

    # ── Minimisation: everything below only removes things ─────────────
    documentation = {
      enable = mkDefault false;
      doc.enable = mkDefault false;
      info.enable = mkDefault false;
      man.enable = mkDefault false;
      nixos.enable = mkDefault false;
    };
    environment.defaultPackages = mkDefault [ ];
    # One locale instead of the full glibc locale archive.
    i18n.supportedLocales = mkDefault [ "C.UTF-8/UTF-8" ];
    # Channels are a stateful, mutable NixOS surface; flakes only.
    nix.channel.enable = mkDefault false;
    programs.command-not-found.enable = mkDefault false;
    # Deploys run as root over SSH; no interactive privilege escalation needed.
    security.sudo.enable = mkDefault false;
    fonts.fontconfig.enable = mkDefault false;
    xdg = {
      autostart.enable = mkDefault false;
      icons.enable = mkDefault false;
      menus.enable = mkDefault false;
      mime.enable = mkDefault false;
      sounds.enable = mkDefault false;
    };
    boot.enableContainers = mkDefault false;

    system.stateVersion = cfg.stateVersion;
    system.nixos.tags = [ "fleetkit-deployer-bootstrap" ];
  };
}
