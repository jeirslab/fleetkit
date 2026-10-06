# Rendering Terraform from the model (`lib.mkTerraform`)

`lib.mkTerraform { fleet, estate }` turns the evaluated model into an
attrset that `builtins.toJSON` writes as a valid `main.tf.json`. It is a pure
evaluation: the kit renders text and never runs tofu or terraform. Plan and
apply stay in the estate repo.

A cluster is the provider's, and its credential with it: the render is done
by whoever owns the cluster, for every estate whose guests are on it, with
the cluster's own token unless an estate sets `placement.tokenRef`.

## What is rendered, and from where

| Output | Source in the model |
| ------ | ------------------- |
| `terraform.required_providers` | `bpg/proxmox` 0.115.0 and `carlpett/sops` 1.4.1, the versions the schema in `providers/schemas/` is pinned to |
| `provider.proxmox` | one provider: `placement.provider` when the estate has a `placement`, otherwise the proxmox provider of the one site its managed guests and pools are on (`fleet.sites.<site>.providers.proxmox`); `endpoint` and `insecure` come from its declaration. Every managed guest and every pool must be on that site; evaluation fails, naming the site and the guest or pool, if one is not, and, with no `placement`, if they are on more than one site or there is nothing to tell the site from |
| `api_token` and `data.sops_file.<alias>` | `placement.tokenRef` when set (an optional override), otherwise the provider's own `tokenRef`: the cluster's credential, a property of the provider and not of the estate. The ref `sops:<estate>/<alias>#<key>` is resolved through the secrets of the estate it names (`fleet.estates.<that estate>.secrets.files`), whichever estate is rendered, to a file path and key. `data.sops_file` is keyed by `<alias>`, or `<estate>_<alias>` when the ref's estate is not the one rendered. Evaluation fails if the ref is malformed, names an undeclared estate, or names a file alias that estate does not declare. The token is always a `${data.sops_file...}` reference, never a literal |
| `resource.<type>.<guest>` | the guest's `providerView`: `providerView.resource` is the type, `providerView.args` the arguments, plus a `lifecycle` block from `prevent_destroy` and `ignore_changes` (omitted when empty) |
| `resource.proxmox_virtual_environment_pool.<pool>` | each entry of `fleet.estates.<e>.pools.proxmox`, with `pool_id` set to the pool name |
| `locals.fleet_unmanaged` | guests whose `mode` is not `managed` (for example `adopted`); they are not rendered |
| `locals.fleet_unrendered_companions` | every guest (managed or not) whose `providerView.companions` is non-empty |

See `guest-model.md` for the guest options and `guest-provider-map.md` for
how each option maps to a provider argument.

## Resource addresses

The address is `<resource type>.<guest name>`, for example
`proxmox_virtual_environment_container.web`. The guest name is the key under
`fleet.guests.<estate>`, so renaming a guest changes its address and
therefore its state. Only `managed` guests get an address.

## Using it from an estate repo

Expose the result as a flake output or a build, and write it out:

```nix
let
  tf = fleetkit.lib.mkTerraform {
    inherit fleet;
    estate = "<estate>";
  };
in
pkgs.writeText "main.tf.json" (builtins.toJSON tf)
```

Copy or link the file into the directory where the estate runs tofu, next to
its own backend configuration. Any `lib` argument is supplied by the kit.

## Not rendered yet

- Companions (for example `lxc_extra_conf`): listed under
  `locals.fleet_unrendered_companions` so they are visible, not silently lost.
- DNS and GitHub resources.
- The state backend block; each estate repo owns its own.
- Anything outside the Proxmox guests and pools: the kit does not run tofu,
  import existing resources or manage state.

## Check

`tests/terraform.sh` renders three estates of a small fixture (own token, the provider's token, no placement) with `mkTerraform`, checks that guests on two sites fail, and
`tests/terraform.py` verifies every resource type and argument against the
pinned provider schema. It runs under the `fidelity` gate in
`tools/gates.sh`.
