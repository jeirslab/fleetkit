# modules/ — NixOS modules shipped by fleetkit-deployer.
#
#   bootstrap.nix            the golden template: the smallest NixOS that
#                            boots, gets an address, and accepts the deploy
#                            key over SSH. Everything else arrives later
#                            (colmena / the deployer pipeline).
#   platform/<target>.nix    one thin layer per image target, adapting the
#                            template to how that platform boots and
#                            networks a guest. Never adds services.
#
# Importing this file gives you the bootstrap module only; platform layers
# are picked per target by lib/default.nix (see `targets`).
{ ... }:
{
  imports = [ ./bootstrap.nix ];
}
