# nixosModules.addons: every NixOS-module add-on of the kit (addons/README.md).
# Importing it changes nothing; each add-on is off until
# fleetkit.addons.<name>.enable is set.
{
  imports = [
    ./pkgs/tailscale
  ];
}
