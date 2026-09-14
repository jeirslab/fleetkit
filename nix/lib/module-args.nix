{ lib, pkgs
  # Flake inputs, threaded so the images-family builder is reachable from
  # the tf emitters and the LXC-template factory module (both build a
  # bootstrap template). Optional: call sites that never touch `.images`
  # (e.g. the collision negative-test in nix/checks.nix) may omit them —
  # the field is lazy and only forces the deployer lib when accessed.
, nixpkgs ? null
, nixos-generators ? null
}:

# fleetkit's PUBLIC helper surface for consumer NixOS modules.
#
# Framework modules import these helpers by relative path
# (`import ../../../lib/sops.nix`), which works inside the framework and is
# useless outside it: a consumer module has no relative path to fleetkit at
# all. Before this existed, a consumer had two bad options — vendor its own
# copy of the helper (which is how a consumer ends up maintaining a stale
# duplicate of sops.nix, drifting silently from the framework's), or
# path-import into the flake input's store path, which is unreadable and
# breaks the moment a file moves.
#
# mkFleet injects this as `_module.args.fleetLib`, so any host module can:
#
#   { config, fleetLib, ... }:
#   let s = fleetLib.sops.withFile ../secrets/btc-nodes.yaml;
#   in { sops.secrets."btc-nodes/mainnet/rpc_password" = s { owner = "bitcoind"; }; }
#
# What belongs here: helpers a CONSUMER module legitimately needs. Not the
# composition functions (mkHosts / mkColmenaNodes) — those are mkFleet's job
# and a host module has no business calling them.

{
  # SOPS secret declaration: mkSecret / withFile / mkTemplate / mkInfisical /
  # mkVaultwarden. `withFile` is what makes a split, per-resource secret store
  # usable from consumer modules.
  sops = import ./sops.nix { inherit lib; };

  # Grafana dashboard + panel builders. Consumer dashboards are written
  # against these, and fleetkit only renders its OWN dashboards internally —
  # so without this a consumer cannot build a dashboard at all.
  grafana = import ./grafana.nix { inherit lib; };

  # Structured PVE Notes rendering (fleet.compute.<host>.note).
  notes = import ./notes/proxmox { inherit lib; };

  # pgweb bookmark construction for consumer database hosts.
  pgweb = import ./pgweb.nix { inherit lib; };

  # Headscale ACL policy construction (the headscale SERVER is consumer-side
  # by design — see ADR-092 — so its policy helper has to be reachable).
  headscalePolicy = import ./headscale-policy.nix { inherit lib; };

  # PVE installer ISO assembly. Needs pkgs, hence this file taking it.
  pveIso = import ./pve-iso.nix { inherit pkgs lib; };

  # Terraform-side builders. These are used from FLEET MANIFEST modules
  # (nix/fleet/**, nix/hosts/** manifest halves), not from NixOS modules —
  # which is why mkFleet injects this surface into the fleet evalModules as
  # well as into every host. A consumer declaring Grafana Cloud synthetic
  # checks needs mkHttpCheck at manifest-eval time, long before any NixOS
  # module argument exists.
  tf = {
    grafana = import ./tf/grafana.nix { inherit lib; };
  };

  # Images component family (mkBootstrapImage / templates / targets). The
  # single source of the bootstrap-template builder and the ADR-0003
  # template references, shared by the tf LXC file-upload emitter
  # (nix/lib/tf/proxmox.nix) and the LXC-template factory
  # (nix/modules/infra/build/lxc-template-factory.nix). Lazy — see the
  # nixpkgs/nixos-generators note in the argument list.
  images = import ../images/deployer/lib { inherit nixpkgs nixos-generators; };
}
