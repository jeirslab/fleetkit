# Rendering Terraform from the model (`lib.mkTerraform`)

`lib.mkTerraform { fleet, estate }` turns the evaluated model into an
attrset that `builtins.toJSON` writes as a valid `main.tf.json`. It is a pure
evaluation: the kit renders text and never runs tofu or terraform. Plan and
apply stay in the estate repo.

## What is rendered, and from where

| Output | Source in the model |
| ------ | ------------------- |
| `terraform.required_providers` | `bpg/proxmox` 0.115.0 and `carlpett/sops` 1.4.1, the versions the schema in `providers/schemas/` is pinned to |
| `provider.proxmox` | one entry per site provider the estate's managed guests and pools use (found through each guest's node, each pool's provider and `fleet.sites.<site>.providers.proxmox`); `endpoint` and `insecure` come from the provider declaration; list form with `alias` when there is more than one |
| `api_token` and `data.sops_file.<alias>` | the estate's `placement.tokenRef` on the provider it is placed on, the provider's own `tokenRef` otherwise (`sops:<estate>/<file>#<key>`), resolved through `fleet.estates.<e>.secrets.files` to a file path and key. The token is always a `${data.sops_file...}` reference, never a literal |
| `resource.<type>.<guest>` | the guest's `providerView`: `providerView.resource` is the type, `providerView.args` the arguments, plus a `lifecycle` block from `prevent_destroy` and `ignore_changes` (omitted when empty) |
| `resource.proxmox_virtual_environment_pool.<pool>` | each entry of `fleet.estates.<e>.pools.proxmox`, with `pool_id` set to the pool name |
| `locals.fleet_unmanaged` | guests whose `mode` is not `managed` (for example `adopted`); they are not rendered |
| `locals.fleet_unrendered_companions` | guests whose `providerView.companions` is non-empty |

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

`tests/terraform.sh` renders a small fixture with `mkTerraform` and
`tests/terraform.py` verifies every resource type and argument against the
pinned provider schema. It runs under the `fidelity` gate in
`tools/gates.sh`.
