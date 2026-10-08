# Deploying with Pulumi and Colmena (experiment)

Branch-only experiment, not for `unstable`. Terraform and OpenTofu are gone
from the deploy path: Pulumi provisions what exists (guests, pools, the GitHub
organisation), Colmena configures what runs on it, and one runner drives both,
from a command line, an HTTP API, or a GitOps server. What is deployed is
declared and type-checked in Nix; nothing is declared in Python or YAML.

```
fleet model ─ lib.pulumi.fromModel ─┐
pulumi.nix  (hand-written stacks) ──┴▶ lib.pulumi.stacks ─▶ pulumi.<stack> ─┐ fleetkit
fleet model ─ lib.mkHive ─────────────────────────────────▶ hives.<estate> ─┘ (CLI / API / GitOps)
```

## Pulumi.nix (`lib.pulumi`, `lib/pulumi/`)

An estate repo declares its stacks as Nix modules, the way NixOS declares a
system:

```nix
# flake.nix
pulumi = fleetkit.lib.pulumi.stacks {
  fleet = config.fleet;                  # the checked model
  modules = [ ./pulumi.nix ];
};

# pulumi.nix
{ fleetkit, config, ... }: {
  imports = [
    (fleetkit.lib.pulumi.fromModel { estate = "homelab"; })                  # stack homelab-guests
    (fleetkit.lib.pulumi.fromModel { estate = "homelab"; github = true; })   # stack homelab-github
  ];
  # What the model does not describe, beside what it does:
  stacks.homelab-guests.resources.backup-pool = {
    type = "proxmox:index/virtualEnvironmentPool:VirtualEnvironmentPool";
    properties = { poolId = "backup"; comment = "PBS targets"; };
    options.provider = "\${provider-proxmox}";
  };
  # Override anything with the module system:
  stacks.homelab-guests.backend = { type = "local"; path = "homelab"; };
}
```

Per stack: `estate` (what `fleetkit deploy <estate>` runs), `packages`,
`resources.<key> = { type; name?; properties; options; adopt?; }`, `variables`,
`outputs`, `backend`. Read-only: `program` (the checked program), `file` (it as
`Pulumi.yaml` in the store, by `builtins.toFile`), `secrets` (the sops
files its invokes read), and `adoptIds` / `adoptUnresolved` (below).

**Types.** Every resource's `properties` are checked against the pinned Pulumi
schema of its `type` (`lib/pulumi/types.nix`). Types are built only for the
types a stack uses: a stack with a container, a VM and pools evaluates in about
0.6 s. Errors are module-system errors at the property's path:

```
error: The option `stacks.mini-guests.resources.extra-pool.properties.poolID' does not exist.
error: A definition for option `stacks.mini-guests.resources.extra-pool.properties.poolId' is not of type `...'
error: stacks.mini-guests.resources.extra-pool.properties: proxmox:index/...:VirtualEnvironmentPool requires poolId
error: stacks.mini-guests: references to no resource or variable: nope
```

Any property also takes a `"${...}"` reference string; references are checked
to name a resource or variable of the stack, not typed. Packages with no pinned
schema are not checked.

## Adoption ids (`adopt`, `adoptIds`, `adoptUnresolved`)

A declaration of something that already exists says so with its provider id,
as data beside the program:

```nix
stacks.homelab-guests.resources.backup-pool = {
  type = "proxmox:index/virtualEnvironmentPool:VirtualEnvironmentPool";
  properties.poolId = "backup";
  adopt = "backup";            # the id the provider imports it by
};
```

- `resources.<key>.adopt` (string or null, default null): the provider id of
  the existing resource this declaration describes. `fleetkit adopt` uses it
  once, to import the resource into the stack's state. It is never rendered
  into the program.
- `<stack>.adoptIds` (read-only): `{ <resource key> = "<id>"; }` for every
  resource whose `adopt` is set.
- `<stack>.adoptUnresolved` (read-only): `{ <resource key> = "<why, and what
  value is needed>"; }` for every resource whose id the model does not
  determine (`resources.<key>.adoptUnresolved`, set by `fromModel`, while its
  `adopt` is null). The command asks for those, or the estate sets
  `resources.<key>.adopt` and the entry is gone.

```sh
nix eval --json <repo>#pulumi.homelab-guests.adoptIds
nix eval --json <repo>#pulumi.homelab-github.adoptUnresolved
```

**No program carries `import`.** `options.import` is not an option:

```
error: stacks.mini-guests.resources.extra-pool.options.import is not supported: an import left in a program destroys the adopted resource on a later `up`. Set stacks.mini-guests.resources.extra-pool.adopt = "<id>" instead: ...
```

and the stack's evaluation checks the rendered program itself for an `import`
option or any adoption field, so no other path can put one there. The reason
is [issue 61]: a container adopted with `options.import` was destroyed by the
next `up` because the `import` was still in the program (Pulumi planned a
replace, and `protect` did not stop it). With the ids outside the program there
is nothing to remember to remove.

`fromModel { estate; github ? false; adopt ? true; }` computes the ids from the
model (they are data, so by default; `adopt = false` leaves every one unset).
An estate overrides one with the module system
(`stacks.<s>.resources.<k>.adopt = lib.mkForce "..."`).

| Resource | id | |
|---|---|---|
| container, VM (bpg/proxmox) | `<node>/<vmid>` | |
| pool | `<pool_id>` | |
| `github_repository` | `<name>` | |
| `github_branch_default` | `<repository>` | |
| `github_membership` | `<org>:<login>` | |
| `github_team` | `<slug>` | unresolved unless the name is in slug form (`[a-z0-9]+(-[a-z0-9]+)*`, not all digits): the provider takes a slug or the numeric id, the model has the name |
| `github_team_members` | `<team slug>` | as the team |
| `github_team_repository` | `<team slug>:<repository>` | as the team |
| `github_organization_settings` | the organisation's numeric id | always unresolved |
| `github_actions_organization_permissions` | `<org>` | |
| `github_repository_environment` | `<repository>:<environment>` (`:` in the environment as `??`) | |
| `github_organization_ruleset` | the ruleset's numeric id | always unresolved |
| `github_issue_label` | `<repository>:<label>` | |
| `github_actions_variable` | `<repository>:<NAME>` | |
| `github_repository_file` | `<repository>:<path>:<branch>` (`:` in the path as `??`; an unset branch is empty: the default branch) | |
| `github_actions_secret` | `<repository>:<NAME>` | the import cannot read the value: the next run writes the model's (an update) |
| `github_actions_organization_secret` | `<NAME>` | the same |

The GitHub formats are those of integrations/github 6.13.0, each cited at its
rule in `lib/pulumi/adopt.nix` (the provider's `docs/resources/<name>.md` and
its importer at tag `v6.13.0`). An id the model does not determine is never
guessed.

## Where the declaring and the type checking happen

1. the model's module types and assertions (`modules/`), at eval;
2. Pulumi.nix: each property against the pinned schema, at eval;
3. `tests/pulumi.sh` / `tests/pulumi_nix.sh` (gates, offline);
4. `pulumi preview`: Pulumi's own check against the running provider.

## State backends (`lib/pulumi/backend.nix`, `cli/fleetkit_cli/backends.py`)

`fromModel` sets each stack's `backend` from the model: the estate's `backend`
(the GitHub stack: `git.backend`). Pulumi.nix can override it per stack. The
runner decrypts what the backend needs with `sops --extract` and registers
every decrypted value for redaction: an event or log line that contains one
shows `[secret]` (Pulumi does print a backend URL it cannot open).

| `fleet.backends` | Pulumi | Tested |
|---|---|---|
| `pg` (`connRef`) | `postgres://` from the decrypted connection string; one `pulumi_state` table, keyed by project and stack, so estates share a database (a `search_path` parameter moves it to a schema). A project in `localStacks` keeps local state. | yes, PostgreSQL 18 |
| `s3`, garage (`host`, `credsRef`, `bucket`) | `s3://<bucket>/<estate>/?endpoint=http://<garage lan address>:3900&region=garage&use_path_style=true`; keys from `credsRef` (`access_key_id`, `secret_access_key`) | yes, garage |
| `s3`, linode (`buckets.<estate>`, `credentials`) | `s3://<bucket>/<keyPrefix>?endpoint=...&region=...&use_path_style=true` | URL form only |
| `local` (`path`) | `file://<state dir>/<path>` (default `pulumi-state`) | yes |
| none | `PULUMI_BACKEND_URL` from the environment, or the deploy fails: never Pulumi Cloud by default | |

Path-style addressing is required for an IP or LAN endpoint (without it Pulumi
asks `<bucket>.<ip>`). The model gained `type = "local"` (with `path`) and a
garage `bucket`; `schemaPrefix` (a tofu notion) is not used.

## The runner (`packages.fleetkit`, `cli/`)

One pipeline for the CLI, the API and GitOps:

1. **render**: `nix eval` of `<repo>#pulumi` (estate, backend, file, secrets
   of each stack). Each stack of the estate gets a project dir, in a
   directory that belongs to this run alone ("A directory per run", below):
   ```
   <state>/runs/<time>-<pid>-<random>/<stack>/
     program      ->  /nix/store/<hash>-Pulumi.yaml   (a GC root; what the estate declares)
     Pulumi.yaml       the program the engine runs: a copy, the unnamed guests protected
     secrets/...  ->  the sops files its invokes read
     Pulumi.<stack>.yaml   Pulumi's settings (the passphrase salt), copied in
     update-plan.json      the plan the up is bound to
   ```
2. **backends**: every stack's backend resolved and its secrets decrypted.
   Steps 1 and 2 finish for every stack before anything runs: a model that does
   not evaluate, or a secret that does not decrypt, changes nothing.
3. **infra**: per stack, the Pulumi Automation API (`install`, then
   `preview --save-plan`), engine events to the event stream, `show_secrets`
   off. The preview's step events become the stack's **plan**: one entry per
   resource that is not `same`. Every stack is previewed, and every plan
   passes the guard (below), before any stack is applied; then
   `up --plan`, stack by stack: the engine is bound to the plan the guard
   read. There is no `up` without one.
4. **nixos**: `colmena apply <goal>` (`build` for a preview) on
   `hives.<hive>` (default: the estate), after infra.

Every deploy's result names each program's store path and sha256
(`programs`, `program_sha256`): what ran, exactly; and `applied`, the stacks
whose `up` finished.

```sh
fleetkit estates
fleetkit preview homelab                 # pulumi preview + colmena build; changes nothing
fleetkit deploy homelab --goal test      # pulumi up + colmena apply test
fleetkit deploy homelab --no-nixos --stack homelab-guests
fleetkit deploy homelab --allow-update web --allow-replace homelab-guests/old-ct   # the guard, below
fleetkit adopt homelab                   # what exists already: "Adopting what already exists"
fleetkit serve                           # the API (below); --repo for GitOps
```

### The guard: what an `up` may do to a guest (`cli/fleetkit_cli/guard.py`)

A summary line with counts (`replace: 1`) does not say what is replaced, and
the estate's own `protect` did not stop the replacement that issue #61 lost a
live container to. So the pipeline every `up` goes through (CLI, API, GitOps,
the action) reads the plan before applying it, and three layers keep the `up`
to what was read: the refusals, the engine's update plan, and `protect`.

- **The plan.** `[{ key, urn, type, op, steps, diff, replaceReasons,
  protected? }]` for every resource whose op is not `same`. `key` is the
  resource's key in the program (for a resource the program no longer has:
  its name in state); `op` is what happens to the resource (`create`,
  `update`, `replace`, `delete`, `import`); `steps` are the engine's ops
  behind it (a replacement is `create-replacement`, `replace`,
  `delete-replaced`); `diff` the changed property paths; `replaceReasons` the
  paths that force the replacement; `protected` is set when the engine itself
  refuses the step because of `protect`. It is in the `plan` event (as data
  and as text), in the `summary` event (`resources`: op → key and type,
  beside the counts), in the job's result (`plan.<stack>`), in the CLI's
  output and in the PR comment.
- **Guests** are every container and VM type of the provider
  (`cli/fleetkit_cli/guests.py`): `virtualEnvironmentContainer`,
  `virtualEnvironmentVm`, `virtualEnvironmentVm2`, `vm`, `clonedVm`,
  `virtualEnvironmentClonedVm` (all `proxmox:index/...`). The list cannot fall
  behind the provider: `tests/guest_types.py` (a gate) and
  `cli/tests/test_guests.py` take every resource token of the pinned name map
  (`providers/pulumi/names/bpg-proxmox-*.json`) whose name matches `vm`,
  `container` or `lxc`, and fail when one is neither a guest nor in the
  explicit not-a-guest list with its reason (the four LVM storage types, which
  match on the `vm` in `Lvm`). A provider bump that adds a guest type fails
  the gate instead of leaving it ungated.
- **Refused**, with nothing applied in any stack of the job, when the plan
  holds for a guest:

  | engine op | named with | API field |
  |---|---|---|
  | `replace`, `create-replacement`, `delete-replaced` (any step of a replacement) | `--allow-replace NAME` | `allow_replace: [NAME]` |
  | `delete` | `--allow-delete NAME` | `allow_delete: [NAME]` |
  | `update` (an in-place update reboots a container) | `--allow-update NAME` | `allow_update: [NAME]` |

  The flags repeat; a name allows that op for that resource only. The error
  lists each offending resource, its op and the flag it needs. A guest's
  `create` is not gated, and nothing is gated for other resources (pools, DNS,
  repositories): they are listed, always.
- **Names are scoped to a stack.** `NAME` is `<stack>/<key>`, a URN, or a bare
  `<key>`. A bare key that is a resource of more than one stack of the request
  (in the program, or still in the state of a stack whose program dropped it)
  is refused as ambiguous, before anything runs, with the two spellings to
  choose from; `<stack>/` must be a stack of the request. A request for one
  stack (`--stack`) takes bare keys as before. When a key exists in two
  stacks, the refusal's flag is already written with the stack
  (`needs --allow-replace homelab-guests/web`).
- **A program with `import` is refused** whatever the flags
  (`options.import` on any resource): after an adoption a leftover `import`
  makes the next `up` replace the resource. Rendered programs never carry one;
  `fleetkit adopt` is how a resource is imported.
- **The `up` is bound to the plan (update plans).** The preview saves the
  engine's own plan (`pulumi preview --save-plan`), the `up` runs on it
  (`pulumi up --plan`), and Pulumi refuses, resource by resource, a step the
  plan does not have: `resource <urn> violates plan: properties changed`,
  `protect changed`, `delete is not allowed by the plan`. So a program or a
  state that changed between the preview and the `up` cannot do more than was
  read. The runner checks first (the sha256 of the program file and of the
  plan file, recorded in memory at plan time, must still match, and the
  targets and `--refresh` must be those of the preview); the engine is the
  check that does not depend on the runner. A violation is a failed job that
  says what had been applied by then. What was seen with Pulumi 3.247 and a
  file backend, offline (`random`, `tls`):

  | | |
  |---|---|
  | a changed program | refused per resource; resources the plan does allow are applied before the one that is refused fails the run (it is a per-resource constraint, not all-or-nothing) |
  | `--target` | the plan and the `up` must have the same targets. A plan made with a target and an `up` without one applies the planned resource and refuses every other change; an `up` with other or fewer targets is refused too. The runner passes the same list to both and refuses a mismatch itself |
  | `--refresh` | works with it on both, on either, or on neither (no drift could be made offline; drift that the `up`'s refresh finds and the plan did not have should be a violation, which is the wanted outcome, but that is not tested) |
  | imports (`fleetkit adopt`) | a plan saved from a preview with `import` and `--target` constrains the `up` that imports; clean imports apply. An untargeted resource *with* `import` is imported whatever the targets; one without is left alone |
  | secrets | values are encrypted in the plan file (`ciphertext`), which is 0600 in a 0700 run directory and deleted after its one `up`. A secret that changed after the plan is a violation. The violation message prints the changed property values, secret outputs in **plain text** (seen with a `RandomPassword` result): the runner strips those values from events, logs and errors (`[values withheld]`) |
  | `protect` | part of the plan: an `up` whose program changed a resource's `protect` is refused (`protect changed`) |
  | a preview that fails | writes no plan; no `up` can follow |

  No combination needed a fallback, and there is none: an `up` without a plan
  is refused in `infra.engine`.
- **Unnamed guests are protected for the run.** The program the engine runs
  is a copy of the estate's (never the estate's file, never the store) in
  which every guest that is not named for a replace or a delete has
  `options.protect = true`. The engine then refuses to replace or delete it
  whatever the plan says, and the state records the flag. Consequences, all
  seen with a real engine:
  - a change of `protect` alone is a `same` step: no update, no reboot;
  - a preview whose plan replaces or deletes a protected guest *fails in the
    engine*, with every step still in its events. The runner reads those and
    reports the usual refusal by name (`protected: true` on the entry); only a
    protection it cannot explain (a resource the estate protects itself, or
    one that is no guest) is passed on as the engine's error;
  - a guest named with `--allow-replace` is not protected in the copy, and the
    engine replaces it although the state still says protected;
  - a guest named with `--allow-delete` that the program no longer has is
    protected in state only, where no program can clear it. Once every
    stack's plan has passed, the runner runs `pulumi state unprotect` on
    exactly those URNs, previews again (the plan must be the one that was
    checked) and applies. If that deploy then fails, they stay unprotected in
    state; the guard still refuses their delete unless named;
  - a `protect` the estate sets is never removed: clear it in the estate.
- **During the `up`** each step is still checked before it runs. If a gated
  step comes up that the request did not name (it would have had to pass both
  layers above), the engine is stopped the hard way ("Stopping a run": two
  signals, then a kill). The step that tripped had started: the job fails and
  says it does not know whether it happened.
- A **preview** (`fleetkit preview`, `preview: true`) prints the same plan and
  marks what a deploy would refuse (`refused.<stack>` in the result); naming
  the resources in the preview request removes the mark.
- A refused deploy is a **failed job** whose `result` still carries `plan` and
  `refused`, so the PR comment (GitOps, or `actions/deploy`) shows the list
  and the commit status reads `deploy failed: refused: replace web`. A deploy
  started by a merge or a push names nothing, so it is refused; apply it by
  hand with the names: `POST /v1/deploys {"estate", "rev", "allow_replace":
  ["homelab-guests/web"]}`, or the action's `allow-replace` / `allow-delete` /
  `allow-update` inputs on a `workflow_dispatch` run.

### A directory per run (`cli/fleetkit_cli/render.py`)

The project dir used to be `<state>/work/<stack>/`, one per stack, whose
`Pulumi.yaml` link every render repointed. A deploy previewed, and then
applied whatever the last render had left there: another job's commit, a
`fleetkit preview`, an adoption, `fleetkit render`. Now:

- every run (a deploy, a preview, an adoption, `fleetkit render`) renders into
  `<state>/runs/<time>-<pid>-<random>/`, which nothing else writes, and
  removes it when it ends (`fleetkit render` keeps its directory and prints
  it; a directory whose process is gone is swept after a day). The Colmena
  hive file is written there too;
- the store path and the sha256 of the estate's program, and the sha256 of the
  copy the engine runs, are recorded when the run renders; before each `up`
  both files are hashed again and the `up` is refused on a mismatch
  (`render.Project.verify`, and again in `infra.apply` for the program and
  the plan file);
- Pulumi's settings file of a stack (`Pulumi.<stack>.yaml`: the passphrase
  salt) is the one thing that outlives a run. It is kept in
  `<state>/stacks/<stack>/` (the first one made stays; a file found in the old
  `work/<stack>/` is taken over) and copied into each run's directory. With
  Pulumi 3.247 and a file backend: without it a run makes a new salt, the
  state's secrets still decrypt (the state carries its own salt and the
  passphrase is the same), and the next `up` re-encrypts the state with the
  new salt; with the kept file the salt stays the same from every directory.

### Stopping a run

`pulumi cancel` is never called. On a self-managed backend (seen with the
file backend; S3 and PostgreSQL are the same code in Pulumi) it only deletes
the stack's lock file and returns success while
the engine keeps running, now unlocked: an unplanned replace ran to completion
that way. The runner starts the `pulumi` process itself, in its own session
(`infra.OwnedPulumi`, given to the Automation API as its `pulumi_command`), and
stops it by signal:

| | the engine (Pulumi 3.247, seen) |
|---|---|
| first cancel (`POST /v1/deploys/{id}/cancel`, Ctrl-C, SIGTERM, SIGHUP): one SIGINT | finishes the step in flight (however long: 9 s for an RSA key in the run that was watched), starts no other, saves the state, releases its own lock, exits. The runner waits for that; it cuts it short by itself only if `FLEETKIT_STOP_GRACE` (seconds) is set |
| second cancel: a second SIGINT | prints `terminating`, but with a provider call in flight it does **not** exit (100 s and counting with the `tls` provider). So 10 s later the runner kills the engine's process group (the engine and its provider plugins) |
| after a kill | the stack's lock file is still there, and the state has a pending operation for the step that was in flight. The runner removes neither and says so: check that no pulumi process runs, then `pulumi cancel` (which then does the one thing it does: remove the lock) |

The lock is the engine's and is left to it. A cancelled job's `result` has
`stopped`: `{stack, verb, why, completed: [{op, key}], in_flight: [{op, key}],
signals, engine, partly_applied, text}`, from the step events, and its `error`
is the sentence: `homelab-guests: the up was cancelled. Stopped after create
web (1 step(s) completed). No step was in flight. The stack may be partly
applied: preview it. The engine was told to stop after the step in flight.`
A step that had started and not finished is listed as in flight, "whether
they happened is not known". The same is said when the engine refuses a step
(a plan violation), when the tripwire stops it, and when an `up` fails.

On the command line SIGINT, SIGTERM and SIGHUP are that cancel (`deploy`,
`preview`, `adopt`): the run ends through its cleanup instead of dying where
it stands, and prints what was done.

`PULUMI_CONFIG_PASSPHRASE(_FILE)` encrypts Pulumi's secrets in state;
`SOPS_AGE_KEY_FILE` decrypts the model's; `FLEETKIT_SECRET_ROOTS` adds
directories (a tenant's source) where sops files are looked up. `pulumi-bin`,
`colmena`, `sops` and `git` come with the package; `nix` is the host's.

### HTTP API (`cli/fleetkit_cli/api.py`; OpenAPI at `/docs`)

| | |
|---|---|
| `POST /v1/deploys` | `{estate, stacks?, rev?, infra, nixos, hive?, on[], goal, preview, refresh, targets[], allow_replace[], allow_delete[], allow_update[]}` (names: `<stack>/<key>`, a URN, or an unambiguous key) → 202 and the job |
| `GET /v1/deploys[/{id}]` | jobs / one job (state, result with rev, programs and their sha256, the plan, what was refused, `applied`, and `stopped` when an up did not finish; error) |
| `GET /v1/deploys/{id}/events?after=&wait=` | events from a sequence number, long-polling |
| `GET /v1/deploys/{id}/stream` | the same as server-sent events, ending with the job record |
| `POST /v1/deploys/{id}/cancel` | the engine is signalled ("Stopping a run"; a second call: the hard way), or colmena terminated. Never `pulumi cancel` |
| `GET /v1/estates` | estates and their stacks |
| `GET /v1/gitops`, `POST /v1/gitops/sync` | GitOps status; fetch and deploy what changed now |
| `POST /v1/hooks/github` | GitHub webhook, `push` and `pull_request`, HMAC-signed (no bearer) |

Bearer auth on every `/v1` route but the webhook; `--no-auth` only on
loopback. One deploy per estate at a time (409 with the running job's id).
Jobs are records plus JSONL event logs; a job cut off by a restart reads
`interrupted`.

## Adopting what already exists (`fleetkit adopt`)

A resource that exists but is not in a stack's state (a container made by
hand, a repository, a pool) is brought in by a command, not by an option in
the program:

```sh
fleetkit adopt homelab                              # the report; changes nothing
fleetkit adopt homelab --stack homelab-guests --resource web --json
fleetkit adopt homelab --id old-ct=pve2/300         # an id the model cannot compute
fleetkit adopt homelab --apply                      # adopt what imports cleanly
fleetkit adopt homelab --apply --accept-update web  # ... and web, updated in place
fleetkit adopt homelab --resource deploy-token --apply   # a secret: only when named
```

Each stack exposes `adoptIds` (`{ <resource key> = <provider import id>; }`)
and `adoptUnresolved` (`{ <resource key> = <why no id>; }`) beside its
`program`; `--id KEY=ID` adds or overrides an id. A kit that does not expose
them yet adopts only by `--id`.

1. The stacks are rendered into this run's own directory and each selected
   stack's state is read (the Automation API's export). **To adopt** =
   resources with an id that are not in state. A resource already in state is
   never touched (the report says so, also when `--id` names it). An
   unresolved resource without `--id` is listed as `cannot adopt: <why>` and
   skipped.
2. **An id that is already in state under another URN is refused**
   (`duplicate`), before any engine run. "In state" used to mean the URN the
   declaration computes to; a renamed resource key (or project, or stack)
   whose id is that of a guest already in state was imported a second time,
   the state then held two entries for one guest, and the next deploy planned
   a delete of the one the program no longer has: the live guest. Now the id
   is compared with the `id` and the `importID` of every state resource of the
   same type; a guest's `<node>/<vmid>` also matches a state id that is the
   vmid. The refusal says how to rename in state instead (`pulumi state
   rename '<urn>' <key>`), and makes the command exit 1 with or without
   `--apply`.
3. A **temporary program** is written to a temporary directory (the system's,
   not the state dir): the stack's program with `options.import` set on the
   resources to adopt, and nothing else changed (every guest in it is
   protected, as in any run).
4. It is previewed with `--target` on those resources and on what they depend
   on (their provider; anything a property refers to). **The report**, per
   resource, its `status`:

   | status | |
   |---|---|
   | `import` | the declaration equals the live resource |
   | `import+update` | it differs: importing it also updates it in place (a guest reboots) |
   | `import+unrecorded` | the only differences are properties the import did not record, so no live value is known and the engine plans an update for them anyway. For a container that update still **reboots** it, even if every value matches; the report and the refusal say so, and `--accept-update` is still required. Known cases for bpg containers and VMs: `cpu`, `memory`, `vmId`, `timeoutStart`, `scsiHardware` |
   | `import+secret` | a GitHub secret named with `--resource`: its value cannot be read back, so adopting it writes the declared value. Naming it is the acceptance |
   | `secret` | a secret that was not named: listed, not adopted (`--resource KEY`) |
   | `absent` | the provider's import finds nothing by this id (`does not exist`, `not found`, 404): "does not exist; a deploy will create it". Not an error, not in the import set, exit 0 |
   | `duplicate` | its id is in state under another URN (2. above) |
   | `other` | the plan would create, replace or delete it |
   | `error` | the import failed for another reason, with the provider's message |
   | `in-state`, `unresolved`, `no-id` | nothing to do; no id |

   A resource that cannot be imported ends the engine's run at that resource:
   the others were not compared (seen with a real engine: resources after it
   get no event at all). So the preview is **run again without** the absent
   and the failed ones, until what is left compares completely; and a
   resource whose import only started (no result event) is never reported as
   a clean `import`.

   Each difference is `{path, live, declared, liveKnown, declaredKnown,
   liveFrom, declaredFrom, kind}`. Both sides are there whenever the engine
   gives them, with `false`, `0` and empty values kept: `liveKnown: false`
   (and no `live` key) is "no value", which is not `live: false`. The live
   value comes from the inputs the import read (`liveFrom: inputs`) or, for
   what those leave out because it equals the zero value (a `false`, a `0`),
   from the state that was read (`state`); the declared one from the
   declaration, or from what the provider will set when it is not declared
   (`provider default`). `kind` is `real` (a live value is known and the
   engine says it differs) or `unrecorded`. Secrets are `[secret]`, like the
   event stream. What the engine sends: an `import` step, then an `update`
   step (or the steps of a replacement) with the live resource as `old`, the
   declaration as `new` and the differing top-level properties in `diffs`.
   Its `detailedDiff` (nested paths, add / delete / update kinds) is used when
   it is there (`engine` in the entry). The pinned Python SDK (3.192) gives
   `diffs` only: it reads the engine's `detailedDiff` under the wrong name
   (`detailed_diff`) and so always drops it, which `infra.py` corrects. Seen
   with a real engine: the kubernetes provider sends `detailedDiff` and then
   *no* `diffs` (without the correction such a change had no paths at all);
   `random` and `tls` send `diffs` and no `detailedDiff`. Which of the two the
   bridged Proxmox and GitHub providers send is not verified; both are read.

   `--json` prints the report as one JSON document on stdout (events go to
   stderr): `{estate, apply, applied, ok, refused[], stacks.<stack>.{program,
   resources[{key, type, urn, id, status, diff[...]}], other[], verify?}}`.
   It is what a person or an agent reads to make the declaration match what
   is there.

   `--resource`, `--id` and `--accept-update` take a resource key. A key that
   two of the selected stacks have is refused as ambiguous: run one stack
   (`--stack`).
5. Without `--apply` that is all: nothing changed.
6. With `--apply`, refusals, all before anything is applied in any stack:
   - a resource that would be `import+update` or `import+unrecorded` and is
     not named with `--accept-update KEY`. For a guest the update is a reboot,
     and the message says so. Fix the declaration, or accept the update;
   - a plan that holds anything but `import`, `update` and `same`: any create,
     replace or delete, of the resource or of something targeted with it (a
     dependency that is not in state yet). The one exception is the `create`
     of a provider or of the stack itself in a new stack: those exist only in
     state;
   - a `duplicate`, and an `error`.

   Then the temporary program is applied with the same targets, **bound to
   the update plan of the preview the report was made from** (as every `up`
   is), the temporary directory is removed, and the **real** program (no
   `import`) is previewed. That check fails, and the command exits non-zero,
   when an adopted resource is not `same`, **or when the preview deletes or
   replaces any guest, or any resource whose state id is one that was just
   adopted**, under whatever URN (`verify.differs`, `verify.destroys`). It
   prints what; it does not try to fix it.
7. `import` is never written anywhere that lasts: only into the temporary
   program, whose directory is removed on every way out (a refusal, an error,
   an interrupt). SIGTERM and SIGHUP are an orderly exit too, like Ctrl-C
   ("Stopping a run"). After a hard kill it stays in the system's temporary
   directory, which no run reads; the next `fleetkit adopt` removes the
   `fleetkit-adopt-<pid>-*` directories of processes that are gone.

Why `import` never stays: with it still in the program after the adoption, the
next `up` planned `replace: 1` for the adopted container and ran
`delete-replaced` on it (issue #61). The runner's guard refuses such a program
for the same reason.

Adoption is a CLI command only: the API's jobs are deploys (one request type,
one runner), so there is no `POST /v1/adoptions`. Run it where the stack's
state is reachable: with a `local` backend, on the deploy server (as its
user, with its state dir); with `pg` or `s3`, from any operator's machine.
Pulumi's own stack lock keeps it apart from a running deploy, and it writes
nowhere a deploy reads.

## GitOps (`fleetkit serve --repo`, `cli/fleetkit_cli/gitops.py`)

The branch is the desired state, and the server deploys it on itself:

- it keeps a bare mirror of the estate repo (`<state>/repo.git`) and runs each
  job from a checkout of one commit (`<state>/checkouts/<sha>`, a shared clone,
  detached; the last few are kept). Nix evaluates a clean tree at a known
  revision, and the job records the sha;
- a push arrives by polling (`FLEETKIT_POLL` seconds) or by the GitHub webhook
  (`FLEETKIT_WEBHOOK_SECRET`); each estate in `FLEETKIT_DEPLOY_ON_PUSH` whose
  last submitted commit is not the head gets a job for the head. A commit is
  submitted once per estate: a failed deploy is fixed forward, or re-run with
  `POST /v1/deploys {"rev": ...}`. A busy estate is picked up next time;
- `FLEETKIT_PUSH_MODE=preview` plans every push (pulumi preview, colmena build)
  and leaves applying to a person: `POST /v1/deploys {"estate", "rev"}`.

### The deploy branch is the gate

GitHub's free plan has no branch protection on private repos, so "merging is
deploying" cannot lean on it. The server holds the line itself: a job that
deploys (not a preview) must be at a commit on its deploy branch
(`FLEETKIT_BRANCH`, the module's `branch`, default `stable`), or it fails with
`<sha> is not on stable`. Previews may be of any commit. With fleetkit's branch
convention (work lands on `unstable` by PR; `stable` is promoted from it), a
token that leaks through a workflow can preview anything and deploy only what
is already on `stable`.

### From GitHub Actions (`actions/deploy`)

The recommended trigger: CI calls the server, so the server needs no inbound
webhook, no GitHub token and no public address. `actions/deploy` is a
composite action (stdlib Python, nothing to install on the runner):

1. with `tailscale-authkey`, it joins the tailnet the server is on: installs
   Tailscale and runs `tailscale up` with `--login-server` (Headscale) and any
   `tailscale-args` (`--accept-dns=false`, `--advertise-tags=tag:ci`, ...),
   and logs out at the end. Use an ephemeral, pre-authorised key;
2. per estate, `POST /v1/deploys` at the commit (a PR's head, or the pushed
   commit), waiting while the estate is busy; follows the job's events into the
   log; writes the step summary (the plan per stack, the program's store path,
   NixOS, the error); on a PR, comments the same, one comment per estate edited
   in place (the workflow's own `GITHUB_TOKEN`, `pull-requests: write`);
3. fails the step if any estate's job did not succeed.

`examples/github/fleetkit.yml` is the estate repo's workflow: PRs into
`unstable` or `stable` preview, a push to `stable` deploys, and
`workflow_dispatch` runs either by hand. Secrets: `FLEETKIT_API_URL`,
`FLEETKIT_API_TOKEN`, `TAILSCALE_AUTHKEY`; variables `TAILSCALE_LOGIN_SERVER`,
`TAILSCALE_ARGS`. GitHub gives no secrets to PRs from forks, so those are
skipped with a notice: the trust boundary comes from GitHub's secret model.
A push made with the default `GITHUB_TOKEN` starts no workflow; a promotion
merged by a GitHub App (fleetkit's `promote.yml`) or by a person does.

On the server's side (`nixosModules.fleetkit-server`): `branch = "stable"`,
`deployOnPush = [ ]` (the workflow names the estates), `poll = 0`, and
`listen = "0.0.0.0:8740"` with `firewallInterface = "tailscale0"` so the API is
open on the tailnet only. The host joins the tailnet as usual
(`services.tailscale`, with `extraUpFlags = [ "--login-server=..." ]` for
Headscale).

### Pull requests through the webhook

The alternative to `actions/deploy`, for a server GitHub can reach. The same
webhook takes `pull_request` events (subscribe the hook to pushes and
pull requests). Opening, pushing to, reopening or marking ready a PR into the
branch previews each estate at the PR's head commit (the mirror fetches
`refs/pull/<n>/head`; PRs into the deploy branch and into
`FLEETKIT_PREVIEW_BRANCHES`), and the result goes back to the PR
(`FLEETKIT_GITHUB_TOKEN`: statuses and issue comments, write):

- a commit status per estate, context `fleetkit/<estate>`: pending while the
  job runs, then success or failure with the change counts
  (`preview succeeded: homelab-guests: create 1`), linking to the job when
  `FLEETKIT_PUBLIC_URL` is set;
- one comment per estate, edited in place on every push to the PR: per stack
  the create / update / replace / delete / same counts and the program's store
  path, NixOS built, or the error.

With `FLEETKIT_TRIGGER=pr` (the NixOS module's default) a merged PR is what
deploys: its merge commit, reported on the PR in the same comment, and pushes
and polling deploy nothing. With `push`, PRs are previewed and the push to the
branch deploys. A preview that arrives while its estate is busy is queued, not
dropped.

Only a PR from a branch of the same repo, by an owner, member or collaborator,
is previewed. A preview runs the PR's own program with the estate's decrypted
credentials, and a program can point a provider at any endpoint (the Proxmox
token would go wherever `endpoint` says), so a fork or an outside author gets
an `error` status ("not previewed: from a fork") and nothing runs. Keep branch
protection on the branch if your plan has it: merging is deploying (the deploy
branch gate above holds either way).

`nixosModules.fleetkit-server` runs it as a hardened systemd service
(`services.fleetkit = { enable; repo; branch; previewBranches; deployOnPush;
trigger; pushMode; poll; publicUrl; listen; firewallInterface; environmentFile;
}`). The host holds
what deploying needs (the age key, Colmena's SSH key, a read-only deploy key,
the Pulumi passphrase, the API token and webhook secret), all from
`environmentFile`, none in the store: treat it like an operator's machine, and
put a TLS proxy in front of anything but loopback.

### Tested

- `cli/tests` (in the package build, offline): the API with a fake runner
  (auth, lifecycle, events and stream, 409, failure, cancel, bad requests,
  restart recovery); GitOps on a local git repo (one submission per commit, a
  busy estate skipped, checkouts at the commit, the webhook's signature, a rev
  through the deploy API); pull requests against a fake GitHub API (previews
  at the PR head, statuses, one comment edited in place, forks, untrusted
  authors, drafts and other bases not previewed, deploy on merge with
  `trigger = pr`, a busy estate's preview queued); redaction.
- `tests/pulumi_nix.sh` (gate): Pulumi.nix good and bad cases above; a
  hand-written `adopt` and `adoptUnresolved`; `options.import` refused with the
  error that names `adopt`.
  `trigger = pr`, a busy estate's preview queued); redaction. With a fake
  engine (`cli/tests/fakes.py`: real Automation API event objects, a state, a
  set of live resources, update plans, `protect`, signals): the guard refuses
  each gated op on a guest and lets it through when named, lists and allows a
  non-guest replace, refuses a program with `import`, refuses the whole job
  when a later stack's plan is refused; every `up` runs the plan of its own
  preview and none runs without one; an `up` that leaves its plan is refused
  with the values withheld; unnamed guests are protected in a copy and the
  estate's file is not touched; protect alone, and the tripwire alone, stop an
  unplanned replace; a named delete of a guest the state protects is
  unprotected, re-planned and applied; an API cancel signals the engine, never
  calls `pulumi cancel`, and the job says which steps finished; another render
  between a deploy's plan and its up changes nothing; a program or a plan
  file changed in between is refused; the settings file is kept and copied
  in; names are scoped to a stack and an ambiguous one is refused before any
  engine runs; `adopt` picks what to adopt (in state, not in state,
  unresolved, `--id`), changes nothing without `--apply`, refuses an
  unaccepted update and any create / replace / delete, refuses an id that is
  in state under another URN, keeps `import` to a temporary program that is
  gone afterwards (also when the engine raises, and on SIGTERM / SIGHUP /
  SIGINT), sweeps what a kill left, reports a `false` live value with both
  sides, reports an absent resource as `absent` and the rest completely,
  classes unrecorded properties and secrets, and fails when the preview after
  the adoption still differs or deletes a guest. Thirty-five hand mutations
  of the fixes for the plan binding, the cancel, protect, the run directories
  and the duplicate-id check are each caught.
- `cli/tests/test_real_pulumi.py` (in the package build; the **real** engine,
  offline: a file backend in a temp dir and the `random` and `tls` providers
  that ship with `pulumi-bin`; a test that cannot run prints a `SKIPPED`
  line with why): a create, a refused and a named replacement and a named
  delete of a protected "guest" (a `RandomString`) through plan-bound ups; a
  program changed after the plan refused by the engine itself, with the
  secret value it prints kept out of events and errors; a cancel in the
  middle of an `up` of eight chained RSA keys stops the engine after the step
  in flight, with the lock still held right after the signal and released by
  the engine, `pulumi cancel` never run, and the next deploy finishing the
  rest; another render and preview between a deploy's plan and its up, and
  one salt and decrypting secrets from every later directory; an adoption
  with real import events (a failing import that does not cut the others
  short, a clean import bound to its plan with `--target`, the same id under
  a renamed key refused, a differing declaration reported with both values);
  a second cancel with a provider call in flight ends in a kill, with the
  lock left and reported; the engine's `detailedDiff` reaching the plan (the
  kubernetes provider rendering YAML to a directory).
- `tests/guest_types.py` (gate) and `cli/tests/test_guests.py`: the guest list
  against the pinned provider's name map.
- `tests/pulumi_nix.sh` (gate): Pulumi.nix good and bad cases above.
- `tests/deploy_e2e.sh` (networked, not a gate): a throwaway estate git repo
  whose pulumi.nix imports the model's stack and adds one composed from it;
  real pulumi previews of both, with state in PostgreSQL and garage
  (`E2E_PG_URL`, `E2E_S3_*`; local otherwise) from sops-encrypted credentials,
  the password-bearing URL never in the events; colmena called on the hive;
  then the server in GitOps mode: a sync plans the head, a signed webhook plans
  the next commit, a bad signature is 401, a pull request is previewed at its
  head and reported (statuses, and a comment with the real plan) to a
  stand-in GitHub API; then the action's client (`actions/deploy`) on the same
  server: a PR preview with its summary and comment, a deploy of a commit not
  on the branch refused, one on it run, a fork's PR skipped; real colmena
  evaluates the hive file. The action's tailnet step is checked by shellcheck
  and the example workflow by actionlint; neither ran on GitHub.

Not tested: `pulumi up` or `colmena apply` against real hosts, a linode bucket,
and the estate repo's own data; `tests/deploy_e2e.sh` was adapted to the run
directories but not run again. The guard and `adopt` ran against the fake
engine and against the real engine with the `random` and `tls` providers, not
against Proxmox or GitHub. What that leaves open, each with how to check it
live on a throwaway guest:

- **The events of the bridged bpg and GitHub providers for an import whose
  inputs differ.** Seen with `random`: an `import` step, then the steps of the
  change with `old` = live, `new` = declared, `diffs` = top-level keys, no
  `detailedDiff`. An in-place `update` after an import could not be made
  offline (every `random` change is a replacement). Check: `fleetkit adopt
  <estate> --resource <throwaway-ct> --json` with one property declared
  wrong; the entry must be `import+update` with that path, `live` and
  `declared` both set.
- **Where a `false` live value is.** The fix reads it from the state the
  import read when the inputs leave it out; that the bridge puts it there for
  `github_repository.allow_rebase_merge` is the report's observation, not
  re-run. Check: adopt a repository with rebase merging off and no
  declaration of it; `allowRebaseMerge` must show `live false`.
- **`unrecorded` for `cpu`, `memory`, `vmId`, `timeoutStart`,
  `scsiHardware`.** Classified by "no live value in what the import read". If
  the provider does record one of them, it is reported as `real` with its
  value, which is right too. Check: adopt a throwaway container declared
  exactly as it is; expect `import+unrecorded` naming those paths, and
  `--accept-update` rebooting it once.
- **How the provider says "does not exist".** `absent` is an error on the
  resource that matches `does not exist`, `not found` or `404` (the engine's
  own `resource '<id>' does not exist` among them); the message is kept in
  `detail`. Check: `fleetkit adopt <estate> --id <key>=<node>/<unused vmid>`
  must report `absent` and exit 0; a wrong API token must be `error`.
- **`protect` on a real guest.** A `protect` change alone was a `same` step
  with `random`; that the bpg provider plans no update for it is assumed
  (protect is the engine's, not the provider's). Check: `fleetkit preview` of
  an unchanged estate after the first deploy with this runner must be all
  `same`.
- **A guest's state id.** The duplicate check takes a container's state id to
  be its vmid and its adoption id `<node>/<vmid>`. Check: `pulumi stack
  export` after adopting the throwaway container (`id`, `importID`), then
  rename its key and run `fleetkit adopt`: it must be `duplicate`.
- **Drift with `--refresh` under a plan.** Check: change a throwaway
  container's description by hand, `fleetkit deploy --refresh`; either the
  preview shows the update (refused unless named) or the `up` is refused as a
  violation. It must never apply an update the plan did not list.
- **What a killed engine leaves on Proxmox.** The first signal lets the step
  in flight finish (seen); after a second the process group is killed, and
  whether a `pct create` or `vzdestroy` already sent completes is Proxmox's
  affair (the task runs on the node, not in the provider). Check: cancel twice during the create of a throwaway
  container, then `pct list` and `fleetkit preview`.
- **Other backends.** Update plans, the lock and the salt were exercised with
  the file backend only; PostgreSQL and S3 use the same self-managed (diy)
  code in Pulumi, so the same is expected. Check: the cancel test's scenario
  (`POST .../cancel` during an `up`) against the PostgreSQL backend, then
  `pulumi stack export` and a second deploy.

## The compiler (`lib.toPulumi`)

- `lib.toPulumi { tf; project; adopt ? { }; }` translates the provider-argument
  stage into a [Pulumi YAML] program, as a Nix attrset; the runner writes it as
  JSON (above).
- `lib.mkPulumi { fleet; estate; adopt ? false; }` and
  `lib.mkGithubPulumi { fleet; estate; adopt ? false; }` are `toPulumi` over
  `lib.internal.guests` and `lib.internal.github`. Every model error those
  raise (two sites, offsite guest, missing token) surfaces unchanged.
- With `adopt` (a map from Terraform type to `name: args: "<id>"` or
  `{ unresolved = "<why>"; }`; `true` on the two wrappers selects the kit's
  maps, `lib/pulumi/adopt.nix`) each resource also carries `adopt` or
  `adoptUnresolved` beside `type`. That is the Pulumi.nix resource shape, what
  `fromModel` hands to `lib.pulumi.stacks`; it is not a `Pulumi.yaml`. Without
  it the result is the program alone. Neither ever has `import`.
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
  a message naming mkPulumi and that the name maps match their generator. No
  render has an `import`, with `adopt = true` either; and as Pulumi.nix stacks
  the fixtures' `adoptIds` and `adoptUnresolved` are, key by key,
  `tests/fixtures/adopt-expected.json` (`tests/pulumi_adopt.py`; gh-mini also
  with `tests/fixtures/gh-adopt`, which renders all fifteen GitHub resource
  types), every resource has an id or a reason, and neither the program nor its
  store file names `import` or `adopt`. Eight
  hand mutations (shape flips, a typo, a literal token, a lost protect, a
  missing resource, a wrong pin, a dangling reference) are each caught.
  The estate `gaps` (every option for adopting existing guests) is rendered
  both ways too: `idmap` becomes the list `idmaps`, `efi_disk` the object
  `efiDisk`, `console` is absent, and each `lifecycle.ignore_changes` must be
  an `options.ignoreChanges` of the same length whose paths start at an input
  property (`clone`, `description`, `operatingSystem`); `outputs` must be the
  render's locals; a string, number or boolean of the wrong type fails.
- `tests/pulumi_preview.sh` (networked, not a gate): `pulumi install` and
  `pulumi preview` of the mini estate with a file backend and a throwaway age
  key. It plans all four resources plus the provider as creates, decrypts the
  token through the bridged sops provider and shows it only as `[secret]`. A
  misspelt property (`vmid`) fails the preview, so the preview really is a type
  check.

Not verified here: an actual `pulumi up` against Proxmox, any adoption id
against a live provider (the formats are the provider's documented ones; no
import was run), a GitHub preview (the provider authenticates against the GitHub
API at configure time), and the estate repo's real renders (its tenant input is
a private repository this environment could not fetch).

## Differences that matter

- **Secrets in state.** Pulumi keeps provider inputs and the sops invoke
  result in state, encrypted by the stack's secrets provider (passphrase, KMS
  or Vault; not age). OpenTofu keeps `data.sops_file` results in state in
  plain text unless its state encryption is configured. So this is a gain, but it needs one more key: a passphrase kept
  in SOPS (`PULUMI_CONFIG_PASSPHRASE`) is the age-only option.
- **State backends.** See "State backends" above.
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
- **Moving an existing estate.** Adoption ids are data, not program: every
  stack exposes `adoptIds` (computed from the model by `fromModel`:
  `<node>/<vmid>`, `<pool_id>`, the GitHub ids above) and `adoptUnresolved`
  (what the model cannot say). `fleetkit adopt` uses them once, to import what
  is already deployed into the stack's state; the program it then previews is
  the one every later `up` runs, with no `import` in it, so there is nothing to
  remember to remove. Terraform state is not converted.
- **Moving an existing estate.** `fleetkit adopt <estate>` ("Adopting what
  already exists"): the bpg import ids come from the model (`<node>/<vmid>`,
  `<pool_id>`) as each stack's `adoptIds`, and the report is the
  model-versus-live check. `options.import` in a program is not the way: on a
  real container the first `up` imported it and updated it in place (a
  reboot), and the next, with `import` still there, destroyed it (issue #61);
  the runner now refuses such a program. Terraform state is not converted.
- **No lxc-conf companion.** The Pulumi path renders nothing for
  `lxcExtraConf`: there is no `terraform_data` and no provisioner (toPulumi
  refuses provisioners). An existing guest keeps its raw lines, which the
  provider neither reads nor writes (`lxc.idmap` excepted: that is the typed
  `idmap`). A guest created from scratch by Pulumi has none of them until they
  are written into `/etc/pve/lxc/<vmid>.conf` by hand. The guests concerned are
  the stack's `outputs.fleet_unrendered_companions`; their `description` is
  under `ignoreChanges`. See `guest-model.md`, "Adopting existing guests".
- **`clone` on an adopted guest.** An import records no `clone`, and every
  member of the block forces a replacement, so a guest that declares
  `source.clone` carries `clone` in `options.ignoreChanges`: used at create,
  not compared afterwards.
- **Ordering, both engines.** A guest's `pool_id` is a plain string, not a
  reference to the pool resource, so neither engine orders the pool first on a
  fresh create. It is unchanged here, to keep the two renders equal.

[Pulumi YAML]: https://www.pulumi.com/docs/iac/languages-sdks/yaml/
[issue 61]: https://github.com/jeirslab/fleetkit/issues/61
