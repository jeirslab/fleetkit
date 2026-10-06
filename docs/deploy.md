# Deploying from the model

The kit evaluates; it never deploys. It exposes two entry points that turn the
guests of one estate into something a deploy tool can consume. Running that
tool (Colmena, or anything else), and the secrets it needs, belong to the
estate repo.

## What each returns

- `lib.mkSystems { fleet; estate; nixpkgs; modules ? [ ]; specialArgs ? { }; }`
  returns `{ <guest> = <nixosSystem>; }`, one system per guest of the estate
  that names a `nixos.module`.
- `lib.mkHive { fleet; estate; nixpkgs; modules ? [ ]; specialArgs ? { };
  network ? "lan"; system ? "x86_64-linux"; }` returns a Colmena hive:
  `{ meta = { nixpkgs; specialArgs; }; <guest> = { imports; deployment; }; }`
  for the same guests. Both build each guest from the same inputs (compute
  map, grants, accounts, root keys, internal domain, module list), so a guest
  evaluates identically either way.

## Wiring it in an estate repo

```nix
{
  outputs = { self, nixpkgs, fleetkit, ... }:
    let
      fleet = fleetkit.lib.fleet { modules = [ ./config.nix ]; };
      args = {
        inherit fleet nixpkgs;
        estate = "example";
        modules = [ ./globals.nix ];
      };
    in
    {
      nixosConfigurations = fleetkit.lib.mkSystems args;
      colmenaHive = fleetkit.lib.mkHive args;
    };
}
```

Then `colmena build --on <guest>` (or `apply`) is run from the estate repo.
Use `network = "mgmt";` in the arguments when guests are reached over a
network other than `lan`.

## Where each `deployment` value comes from

| Value                   | Source                                                          |
| ----------------------- | --------------------------------------------------------------- |
| `deployment.targetHost` | the guest's address on `network` (`guest.ipv4.<network>`)       |
| `deployment.targetUser` | `fleet.estates.<estate>.colmena.targetUser`, default `root`     |
| `deployment.tags`       | the guest's `tags`, plus its `kind` and the estate name         |

A guest with no address on `network` makes evaluation fail with a message
naming the guest and the network; nothing falls back to a guess.

## `deployment` inside and outside a hive

Inside a hive, `deployment` is Colmena's own option, declared by Colmena. The
shared base (`nixos/base.nix`) declares a loose `deployment` option only
outside a hive (`mkSystems`), so modules that set `deployment.*` evaluate in
both. It is never declared twice.

## Node systems (bare-metal machines)

A node of a site is a NixOS machine when it names a `nixos.module`
(`fleet.sites.<site>.nodes.<node>.nixos.module`, default null). A node with no
module is not built. Pass `site` instead of `estate`; passing both, or
neither, throws.

- `lib.mkSystems { fleet; site; nixpkgs; ... }` returns `{ <node> = <nixosSystem>; }`
  for the site's nodes that name a module.
- `lib.mkHive { fleet; site; nixpkgs; ... }` returns a hive with the same
  nodes. `deployment.targetHost` is the node's `address`, `targetUser`
  defaults to `root`, and `deployment.tags` is `[ "machine" <site> ]`.

What the base (`nixos/base.nix`) supplies for a machine: the host name (the
node's name), sshd, operator accounts and root keys, the `fleet.*` facts
(`fleet.compute` is the site's nodes, name to `internal_ip`; the internal
domain is null unless the site's network names one), grants by the site's
region and grants with `where = null`, the loose `deployment` option outside a
hive, and the inert `proxmoxLXC` option.

What it does not supply: no platform profile (neither the proxmox-lxc profile
nor the qemu guest profile), and no default root file system or boot loader.
The node's own module brings its hardware configuration, file systems and
boot loader. Hardware modules, disk layout and installers are not part of the
kit.

```nix
{
  outputs = { self, nixpkgs, fleetkit, ... }:
    let
      fleet = fleetkit.lib.fleet { modules = [ ./config.nix ]; };
      args = { inherit fleet nixpkgs; site = "example"; };
    in
    {
      nixosConfigurations = fleetkit.lib.mkSystems args;
      colmenaHive = fleetkit.lib.mkHive args;
    };
}
```

## What the kit does not do

Nothing here runs Colmena, builds an estate, holds secrets or reaches a host.
Both functions are evaluation only.
