# addons/

Add-ons a host or an estate switches on. This folder holds the NixOS-module kind; the
estate kind (Ansible, `fleet.estates.<estate>.addons.<name>`) is in `modules/addons.nix`
and `docs/addons.md`.

## addons/nix/

NixOS modules a host imports.

- One folder per add-on: `addons/nix/pkgs/<name>/default.nix`.
- Each is a NixOS module. Its options live under one namespace,
  `fleetkit.addons.<name>`, and it does nothing until `fleetkit.addons.<name>.enable`
  is true.
- `addons/nix/default.nix` imports them all. The flake exposes that as
  `nixosModules.addons` and each one as `nixosModules.addons-<name>`.
- Tests: `tests/nix-addons.sh` (evaluation only; run by `tools/gates.sh`).

Add-ons: `tailscale` (the client, the node certificate, the Headscale DNS-01 settings).
