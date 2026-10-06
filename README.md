# fleetkit

The schema and checks of the fleet model. No data lives here: an estate repo
supplies it and consumes this as a flake input.

```nix
inputs.fleetkit.url = "github:jeirslab/fleetkit/unstable";

outputs = { fleetkit, xgcs, ... }: {
  fleet = fleetkit.lib.fleet {
    modules = [ ./config.nix ./allow.nix ];   # the lab's own data
    tenants = { xgcs = "${xgcs}"; };          # repos that declare an estate
  };
};
```

- `modules/` the typed model (sites, estates, guests, operators, secrets,
  zones, repos, backends); `lib/` the entry points and the tenant boundary.
- `nixos/base.nix` and `lib/systems.nix` assemble a guest's NixOS system from
  the model plus the module the guest names (`lib.mkSystems`).
- Repositories: an estate declares its GitHub organisation and repositories
  in the model (`fleet.estates.<e>.git`, `fleet.repos.<e>`). That is the
  GitHub management plane only; nothing is cloned or vendored from it.
- `providers/` pinned provider schemas the guest model is checked against.
- `docs/` the model's decisions: `guest-model.md`, `guest-provider-map.md`,
  `tenants.md`, `schema-todo.md` (the inventory of untyped blocks).
- `tools/gates.sh` parse, eval, lint and provider fidelity. Checks that need
  real data (negative cases, parity) run in the estate repo.

## Working on issues

Work lands on `unstable` without a pull request. `/issue-plan <n>` posts a
plan on an issue; the templates in `.claude/workflows/` run it in an outside
worktree (`~/worktrees/fleetkit/issue-<n>`), each ending with a review;
`issue-converge` lands the approved branches and closes the issues. No
workflow deploys or uses production credentials. An estate repo that
consumes the kit picks a change up with `nix flake update fleetkit`.
