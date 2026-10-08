# Tenants: declare there, validate and deploy here

The lab (this repo) owns the hardware, the networks, the grants, the state
backends and the credentials. A tenant owns what it wants deployed. The xgcs
estate is the first tenant; its declaration lives in
`XG-Capital-Strategies/deployments` (`./fleet`), not here.

## How it is wired

- `flake.nix` takes the tenant repo as a plain source input (`flake = false`)
  and passes `tenants = { xgcs = <source>; }` to `lib.mkFleet`, which adds
  `<source>/fleet` to the modules. The tenant repo never imports this one, so
  there is no cycle.
- `config.nix` keeps only the grant for xgcs: backend, placement (provider,
  pool, token), guest defaults, colmena user, substrate/gpu/llm/storage and
  the pool with its vmid and host ranges.
- The tenant sets, for its own estate only: owner, domains, tailnets,
  environments, secrets (file registry), observability, git (the GitHub
  organisation); plus its guests, its repositories and its people.
- A guest names what runs in it with `nixos.module` (a file in the tenant
  repo). The model carries the path; the deploy layer will assemble the system
  from the lab's base plus that module.

## What is checked

1. Ownership, on the definitions themselves (`lib/default.nix`,
   `tenantViolations`): a file under a tenant source may set only
   `fleet.estates.<tenant>.<owned attribute>`, `fleet.guests.<tenant>`,
   `fleet.repos.<tenant>` and principals the lab does not already define.
   Anything else fails evaluation, `mkForce` included.
2. The grant (`modules/guests.nix`, `grantAssertions`): a guest's pool is a
   pool of its own estate, its vmid is inside the pool's vmid ranges and its
   address inside the pool's host ranges. A guest with `pool = "none"` (in no
   pool) is held to the ranges of the estate's `placement.pool`.
3. Everything the model already checked (references, collisions, secret refs
   per estate, storage on the node, ...), now across both repos.

`tests/negative/cases-tenant.json` holds one failing mutation per rule; the
runner copies the tenant slice next to the lab copy (`.xgcs/fleet/...`).

## Changing a tenant

A change is a pull request in the tenant repo. Here: `nix flake update xgcs`,
then `tools/gates.sh`. To try an unmerged branch:
`nix eval .#fleet --override-input xgcs path:<checkout>`.

A tenant change that uses options of a newer kit needs the lab's fleetkit
lock bumped first: the lab evaluates the tenant's declaration with the lab's
kit, so an option the lab's kit does not have is an evaluation error. For the
GitHub options (`repos.<tenant>.<key>.actions.*`, `labels`, `files`,
`runners`) and what the kit bump stops rendering, the order across the two
repositories is in `docs/github.md`, "A tenant: two repositories, a fixed
order".
