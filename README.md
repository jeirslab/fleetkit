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
- `lib.mkHive` builds a Colmena hive from the same guests (target host, user
  and tags from the model); see `docs/deploy.md`. Nothing in the kit deploys.
  Both also take a `site` instead of an `estate` to build a site's bare-metal
  nodes that name a `nixos.module`.
- Repositories: an estate declares its GitHub organisation and repositories
  in the model (`fleet.estates.<e>.git`, `fleet.repos.<e>`). That is the
  GitHub management plane only; nothing is cloned or vendored from it.
- `lib.pulumi` (Pulumi.nix): an estate's Pulumi stacks as Nix modules, each
  resource's properties typed from the pinned schemas, the model's stacks
  imported with `fromModel`, backends from `fleet.backends`. Experimental; see
  `docs/pulumi.md`.
- `lib.mkPulumi` / `lib.mkGithubPulumi` render an estate's guests and pools,
  and its GitHub organisation, as Pulumi YAML programs; Pulumi runs the pinned
  Terraform providers through its terraform-provider bridge. `lib.internal`
  holds the provider-argument stage they compile from (`docs/terraform.md`,
  `docs/github.md`). Experimental; see `docs/pulumi.md`.
- `packages.fleetkit` (`cli/`) deploys an estate: `pulumi up` on its programs,
  then `colmena apply` on its hive, from the command line or an HTTP API
  (`fleetkit serve`), or as a GitOps server that deploys the estate repo's
  branch (`fleetkit serve --repo`, `nixosModules.fleetkit-server`).
  Experimental; see `docs/pulumi.md`.
- `providers/` pinned provider schemas the guest model is checked against.
- `docs/` the model's decisions: `guest-model.md`, `guest-provider-map.md`,
  `deploy.md`, `tenants.md`, `schema-todo.md` (the inventory of untyped blocks).
- `docs/secrets.md` host keys, operators and readers in the model, the
  rendered and checked `.sops.yaml`, and the re-key recipe. The kit never
  decrypts.
- `tools/gates.sh` parse, eval, lint and provider fidelity. Checks that need
  real data (negative cases, parity) run in the estate repo.

## Working on issues

Work lands on `unstable` without a pull request. `/issue-plan <n>` posts a
plan on an issue; the templates in `.claude/workflows/` run it in an outside
worktree (`~/worktrees/fleetkit/issue-<n>`), each ending with a review;
`issue-converge` lands the approved branches and closes the issues. No
workflow deploys or uses production credentials. An estate repo that
consumes the kit picks a change up with `nix flake update fleetkit`.
