# Deploying with Pulumi and Colmena (experiment)

Branch-only experiment, not for `unstable`. Terraform and OpenTofu are gone
from the deploy path: Pulumi provisions what exists (guests, pools, the GitHub
organisation), Colmena configures what runs on it, and one runner drives both,
from a command line or an HTTP API. The model, its checks and every render
stay pure Nix.

```
fleet model ─ lib.internal.guests ─▶ provider args ─ lib.toPulumi ─▶ pulumi.<estate>.guests ─┐
            ─ lib.internal.github ─▶ provider args ─ lib.toPulumi ─▶ pulumi.<estate>.github ─┤ fleetkit
            ─ lib.mkHive ──────────────────────────────────────────▶ hives.<estate> ─────────┘ (CLI / API)
```

## Where the declaring and the type checking happen

Nothing is declared outside Nix. The Pulumi programs are not written in a
Pulumi language: they are Pulumi YAML, which is data, rendered by `nix eval`
the way a derivation is built, and the runner only runs them. So the checks
are all on the Nix side or on the rendered artefact, in this order:

1. the model's module types (`modules/`) and its assertions, at eval;
2. the name maps: a provider argument the pinned provider does not have fails
   the render (`lib/pulumi.nix`), at eval;
3. `tests/pulumi.sh` (gate, offline): every program against the pinned Pulumi
   schemas, property by property, list or object;
4. `pulumi preview`: Pulumi's own type check against the running provider.

The gap is the model's untyped blocks (`docs/schema-todo.md`): their contents
are checked at steps 2 to 4, not by a Nix option type. Generating
`types.submodule`s from the pinned schemas would move that to step 1.

## The runner (`packages.fleetkit`, `cli/`)

One pipeline, the same for the CLI and the API:

1. **render**: `nix eval --json <repo>#pulumi.<estate>.<stack>` for every
   stack, each into a project dir. All stacks render before anything runs, so
   a model that does not evaluate changes nothing.
2. **infra**: per stack, the Pulumi Automation API (`install`, then `preview`
   or `up`). Engine events (each resource step, diagnostics, the summary) go
   to the event stream. `show_secrets` is off: `up()` defaults it on.
3. **nixos**: `colmena apply <goal>` (or `build` for a preview) on
   `hives.<hive>` (default: the estate), read through a one-line `hive.nix`.
   It runs after infra, so the hive is evaluated against what was provisioned.

```sh
fleetkit estates
fleetkit preview homelab                 # pulumi preview + colmena build; changes nothing
fleetkit deploy homelab --goal test      # pulumi up + colmena apply test
fleetkit deploy homelab --no-nixos --stack guests
fleetkit serve --listen 127.0.0.1:8740   # token from FLEETKIT_API_TOKEN(_FILE)
```

It refuses to run without `PULUMI_BACKEND_URL` (no silent Pulumi Cloud) and
without `PULUMI_CONFIG_PASSPHRASE(_FILE)`. `SOPS_AGE_KEY_FILE` decrypts the
model's secrets; `FLEETKIT_SECRET_ROOTS` adds directories (a tenant's source)
where the programs' sops files are looked up. `pulumi-bin`, `colmena` and
`sops` come with the package; `nix` is the host's.

### HTTP API (`cli/fleetkit_cli/api.py`; OpenAPI at `/docs`)

| | |
|---|---|
| `POST /v1/deploys` | start a deploy: `{estate, stacks?, infra, nixos, hive?, on[], goal, preview, refresh, targets[]}` → 202 and the job |
| `GET /v1/deploys[/{id}]` | jobs / one job (state, result, error) |
| `GET /v1/deploys/{id}/events?after=&wait=` | events from a sequence number, long-polling |
| `GET /v1/deploys/{id}/stream` | the same as server-sent events, ending with the job record |
| `POST /v1/deploys/{id}/cancel` | Pulumi's own cancel, or colmena terminated |
| `GET /v1/estates` | estates and stacks |

Every `/v1` route needs `Authorization: Bearer <token>`; `--no-auth` is
refused off loopback. One deploy per estate at a time (409 with the running
job's id), several estates at once. Jobs are records plus append-only JSONL
event logs under the state dir; a job that was running when the server
stopped reads `interrupted` on restart. Why this and not tofu behind a web
hook: the Automation API gives structured per-resource events and a cancel
that leaves state consistent, and the pipeline is a function the CLI and the
server share, not a subprocess the server scrapes.

### Tested

- `cli/tests/test_api.py` (runs in the package build, offline, fake runner):
  auth, lifecycle, events and the stream, 409, failure, cancel, bad requests,
  restart recovery.
- `tests/deploy_e2e.sh` (networked, not a gate): a throwaway estate flake on
  this checkout; `fleetkit estates`; `fleetkit preview` (a real pulumi preview,
  6 creates, the token never in the events; colmena called as `build -f
  <hive.nix>`); the same through `fleetkit serve` with curl (401, 202, job
  succeeded, stream end, an unknown estate's job failed with the render
  error); and real colmena evaluating the runner's hive file.

Not tested: a `pulumi up` or `colmena apply` against real hosts, and the
estate repo's own data (its tenant input could not be fetched here).

## The compiler (`lib.toPulumi`)

- `lib.toPulumi { tf; project; adopt ? { }; }` translates the provider-argument
  stage into a [Pulumi YAML] program. JSON is YAML, so `builtins.toJSON` of the
  result is a valid `Pulumi.yaml`.
- `lib.mkPulumi { fleet; estate; adopt ? false; }` and
  `lib.mkGithubPulumi { fleet; estate; }` are `toPulumi` over
  `lib.internal.guests` and `lib.internal.github`. Every model error those
  raise (two sites, offsite guest, missing token) surfaces unchanged.
- The stage keeps the pinned Terraform providers' own argument names because
  those providers are what runs (through the bridge), and their schemas are
  what the guest model is checked against (`docs/guest-provider-map.md`).

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
  a message naming mkPulumi and that the name maps match their generator. Eight
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
