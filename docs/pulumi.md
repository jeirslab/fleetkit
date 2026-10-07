# Pulumi instead of OpenTofu (experiment)

Branch-only experiment, not for `unstable`: can the deploy engine be Pulumi
while the model, its checks and the render stay pure Nix? Short answer: yes,
and it plugs in after the Terraform render without touching the model.

## How it plugs in

```
fleet model ──lib.mkTerraform──▶ main.tf.json attrset ──lib.toPulumi──▶ Pulumi.yaml (JSON)
            ──lib.mkGithubTerraform──▶       "         ──lib.toPulumi──▶      "
```

- `lib.toPulumi { tf; project; adopt ? { }; }` translates any rendered
  `main.tf.json` attrset into a [Pulumi YAML] program. JSON is YAML, so
  `builtins.toJSON` of the result is a valid `Pulumi.yaml`. It is evaluation
  only, like the Terraform render.
- `lib.mkPulumi { fleet; estate; adopt ? false; }` and
  `lib.mkGithubPulumi { fleet; estate; }` are `toPulumi` over `mkTerraform` and
  `mkGithubTerraform`. Every throw those raise (two sites, offsite guest,
  missing token) is raised unchanged, so both engines plan the same model.
- Pulumi YAML was chosen over a TypeScript/Python/Go program on purpose: the
  program is data, Nix stays the only language, and the render stays a pure
  eval. No Node or Python toolchain is needed; the YAML language host ships
  inside `pulumi-bin`.

## Providers: the same binaries, at the same pins

Each Terraform provider runs through Pulumi's `terraform-provider` bridge,
parameterised with the exact source and version the render pins:

```yaml
packages:
  proxmox: { source: terraform-provider, version: 1.4.0, parameters: [bpg/proxmox, 0.115.0] }
  sops:    { source: terraform-provider, version: 1.4.1, parameters: [carlpett/sops, 1.4.1] }
```

So the provider code that runs is the code the guest model is already checked
against (`providers/schemas`), not a separately versioned Pulumi port
(muhlba91/pulumi-proxmoxve lags bpg, and there is no first-party sops package).

The bridge renames fields, and not only by camelCasing:

| Terraform | Pulumi | rule |
|---|---|---|
| `network_interface` (list) | `networkInterfaces` | lists are pluralised |
| `disk` (one-item block, container) | `disk` object | one-item lists become objects |
| `disk` (list, VM) | `disks` | |
| `id` (SDKv2 optional+computed) | `teamId`, `githubIssueId` | an input `id` becomes `<resource>Id` |
| `${github_team.x.id}` | `${x.id}` | a reference to `id` is the Pulumi resource id |

`tests/gen_pulumi_names.py` pairs each pinned Terraform schema with the
bridge's Pulumi schema (`providers/pulumi/schemas`, from
`pulumi package get-schema terraform-provider@1.4.0 <source> <version>`) and
writes `providers/pulumi/names/<provider>-<version>.json`. Every Terraform name
of all three pinned providers (116 + 88 resources, 168 data sources) is
matched. `lib/pulumi.nix` reads only the name maps, and an argument they do not
know fails evaluation.

## Translation

| Terraform JSON | Pulumi YAML |
|---|---|
| `resource.<type>.<name>` | `resources.<name>`, logical name = Terraform name (key `<name>_<type>` plus `name:` when two types share a name, e.g. GitHub's `org`) |
| `provider.<p>` | `resources.provider-<p>` (`pulumi:providers:<pkg>`), set as `options.provider` |
| `data.sops_file.<n>` | `variables.sops_file_<n>: fn::invoke sops:index/getFile:getFile` |
| `${data.sops_file.n.data["k"]}` | `${sops_file_n.data["k"]}` |
| `lifecycle.prevent_destroy` | `options.protect` |
| `lifecycle.ignore_changes` | `options.ignoreChanges` (renamed paths) |
| `depends_on` | `options.dependsOn` |
| `locals` | `outputs` |
| `count`, `for_each`, provisioners, other lifecycle keys | refused with a throw (none are rendered on `unstable`) |

## What was verified

- `tests/pulumi.sh` (in `tools/gates.sh`, fidelity; offline): renders every
  tf-mini estate and gh-mini both ways and checks each Pulumi program against
  the pinned Pulumi schemas directly (not through the name maps, so a wrong map
  is caught). It checks that properties and list/object shapes exist, that
  resources match the Terraform render one to one by token and logical name,
  that `prevent_destroy` becomes `protect`, that packages are the bridge at the
  pins, that every reference resolves, and that provider credentials are sops
  invoke references. It also checks that `split`/`offsite` are refused with
  mkTerraform's message and that the name maps match their generator. Eight
  hand mutations (shape flips, a typo, a literal token, a lost protect, a
  missing resource, a wrong pin, a dangling reference) are each caught.
- `tests/pulumi_preview.sh` (networked, not a gate): `pulumi install` and
  `pulumi preview` of the mini estate with a file backend and a throwaway age
  key. It plans all four resources plus the provider as creates, decrypts the
  token through the bridged sops provider and shows it only as `[secret]`. A
  misspelt property (`vmid`) fails the preview, so the preview really is a type
  check.

Not verified here: an actual `pulumi up` against Proxmox, `adopt` against a
live estate, a GitHub preview (the provider authenticates against the GitHub
API at configure time), and the estate repo's real renders (its tenant input is
a private repository this environment could not fetch).

## Differences that matter

- **Secrets in state.** Pulumi keeps provider inputs and the sops invoke
  result in state, encrypted by the stack's secrets provider (passphrase, KMS
  or Vault; not age). OpenTofu keeps `data.sops_file` results in state in
  plain text unless its state encryption is configured. So this is a gain, but it needs one more key: a passphrase kept
  in SOPS (`PULUMI_CONFIG_PASSPHRASE`) is the age-only option.
- **State backends.** `pulumi login` takes `file://`, `s3://`, `gs://`,
  `azblob://` and `postgres://`, so `fleet.backends` maps over. The rendered
  program does not name a backend, just as the Terraform render does not yet
  (#10).
- **Pulumi Cloud is the default.** Without `PULUMI_BACKEND_URL` or
  `pulumi login`, some commands fall back to Pulumi Cloud; in this experiment
  `pulumi package add` created an ephemeral Pulumi Cloud agent account. A
  runner must always set the backend.
- **Plugins are fetched, not built by Nix.** `pulumi install` downloads the
  bridge from get.pulumi.com and the provider binaries from the OpenTofu
  registry, the same trust and hermeticity as `tofu init` today. Pin the bridge
  version: resolving "latest" goes through api.github.com. nixpkgs has
  `pulumi-bin` and the Terraform providers (`terraform-providers.*`), but not
  the bridge, so a fully Nix-built plugin cache would need a derivation for
  `pulumi-terraform-provider`.
- **Parameterised packages need `pulumi install`** before `preview` or `up`.
- **Moving an existing estate.** `mkPulumi { adopt = true; }` sets
  `options.import` on every guest and pool, with the bpg import ids computed
  from the model (`<node>/<vmid>`, `<pool_id>`), so the first `pulumi up`
  adopts instead of creating. Pulumi refuses an adoption whose inputs differ
  from the live resource, so that run doubles as a model-versus-live check.
  Drop `adopt` after it. Terraform state is not converted.
- **Ordering, both engines.** A guest's `pool_id` is a plain string, not a
  reference to the pool resource, so neither engine orders the pool first on a
  fresh create. It is unchanged here, to keep the two renders equal.

[Pulumi YAML]: https://www.pulumi.com/docs/iac/languages-sdks/yaml/
