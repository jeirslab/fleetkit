# nix/components/registry.nix — the component catalog.
#
# Every componentized leaf's `mkComponent` descriptor, listed EXPLICITLY (no
# readDir — matches fleetkit's hand-listed aggregation style and keeps the
# "untracked files invisible to flake eval" gotcha honest). Grouped by
# family; empty groups are valid. Grows as leaves are componentized (M1+).
{ lib }:

let
  mkComponent = import ../lib/mkComponent.nix { inherit lib; };
in
{
  modules = [
    (mkComponent {
      family = "module";
      name = "infra.network.dns";
      src = ../modules/infra/network/dns;
      requires = [ "options" ];
    })
    # Multi-file leaf: options are declared across default.nix (.postgresql),
    # pgbackrest.nix (.backup) and pgweb-access.nix (.pgwebAccess). No
    # options/ split needed — the schema buckets every option path declared
    # anywhere under `src`, so `requires = []` is enough to lock its interface.
    (mkComponent {
      family = "module";
      name = "infra.data.postgresql";
      src = ../modules/infra/data/postgresql;
      requires = [ ];
    })
  ];
  tf = [ ]; # M3
  images = [ ]; # M4
}
