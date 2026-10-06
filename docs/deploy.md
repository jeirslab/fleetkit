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

## What the kit does not do

Nothing here runs Colmena, builds an estate, holds secrets or reaches a host.
Both functions are evaluation only.
