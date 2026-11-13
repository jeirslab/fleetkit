# nixosModules.secrets (ADR-0009, ADR-0017): sops-nix, and the identity a host decrypts
# with: its SSH host key, as fleet sets it on every host today
# (sops.age.sshKeyPaths = [ "/etc/ssh/ssh_host_ed25519_key" ]). Set with
# mkDefault, so a host that sets it keeps its own value.
sops-nix:
{ lib, ... }:
{
  imports = [ sops-nix.nixosModules.sops ];
  sops.age.sshKeyPaths = lib.mkDefault [ "/etc/ssh/ssh_host_ed25519_key" ];
}
