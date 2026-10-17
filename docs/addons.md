# Estate add-ons, and the Ansible add-on

An add-on is a provider or runner an estate switches on in its own
declaration, `fleet.estates.<estate>.addons.<add-on>` (`modules/addons.nix`).
Every add-on is off by default. An estate that switches none on renders
exactly as before: nothing of an add-on is in its guests render, its Pulumi
program or its stack (`tests/addons.sh` checks this for every other estate of
the fixture). `addons` is tenant-owned (`lib/default.nix`, `tenantOwned`): a
tenant switches add-ons on for its own estate, as it declares its guests.

There is one add-on so far, `ansible`.

## The Ansible add-on

It runs Ansible playbooks against the estate's guests as part of a deploy,
through Pulumi: each playbook is an `ansible_playbook` resource of the
`ansible/ansible` Terraform provider (1.5.0, pinned in `providers/` like the
others: its Terraform schema, its bridge schema and name map), in the estate's
guests stack (`<estate>-guests`), after its guest's resource. A deploy that
creates the guest therefore creates it first and then runs the playbook. It
is how jeirslab/fleet's ADR-0013 configures the VyOS router guest
(`vyos.vyos`).

```nix
fleet.estates.lab = {
  colmena.targetUser = "vyos";        # the SSH user of every deploy of the estate
  addons.ansible = {
    enable = true;
    playbooks.router = {
      guest = "vyos";                 # fleet.guests.lab.vyos
      playbook = ./ansible/router;    # a Nix path or derivation: a file, or a directory
      entry = "site.yml";             # the playbook inside that directory
      collections = [ "vyos.vyos" "ansible.netcommon" ];
      groups = [ "routers" ];
      extraVars = {
        ansible_connection = "ansible.netcommon.network_cli";
        ansible_network_os = "vyos.vyos.vyos";
        ansible_ssh_common_args = "-o StrictHostKeyChecking=accept-new";
      };
      # A file on the deploy runner (sops-nix there, say): only the path is rendered.
      secretFiles.vyos_password_file = "/run/secrets/vyos-admin";
    };
  };
};
```

### Options (`fleet.estates.<estate>.addons.ansible`)

| Option | Type | Default | |
|---|---|---|---|
| `enable` | bool | `false` | switches the add-on on; playbooks declared while it is off are refused |
| `playbooks.<name>.guest` | string | | the guest's name in this estate |
| `playbooks.<name>.playbook` | path (a Nix path or derivation) | | the playbook file, or a directory holding it |
| `playbooks.<name>.entry` | null or string | `null` | the playbook inside `playbook` when that is a directory (relative, no `..`) |
| `playbooks.<name>.collections` | list of strings | `[ ]` | the collections the playbook uses; each must be one the runner has |
| `playbooks.<name>.network` | string | `"lan"` | the network whose static address of the guest is `ansible_host` |
| `playbooks.<name>.user` | null or string | `null` | `ansible_user`; null = the estate's `colmena.targetUser`, else `root` |
| `playbooks.<name>.groups` | list of strings | `[ ]` | inventory groups of the guest for the run |
| `playbooks.<name>.extraVars` | attrs of strings | `{ }` | extra variables; never a secret: they are in the program and the state |
| `playbooks.<name>.secretFiles` | attrs of strings | `{ }` | extra variables whose value is the absolute path of a secret file on the runner, outside the store |

Evaluation refuses (each with the option's path in its message;
`tests/cases-addons.json`): playbooks while `enable` is false, an add-on the
kit does not have, a guest that is not the estate's, a guest without a static
address on `network`, a playbook that is not in the store (a plain path
string), an `entry` outside the directory, a collection the runner does not
have, a secret file that is not an absolute path outside the store (a value
pasted in, a store path), `ansible_host` / `ansible_user` / `hostname` in the
variables (fleetkit's and the provider's), the same name in `extraVars` and
`secretFiles`, and a playbook named like a guest, pool or upload of the
estate (the program keys resources by name, and a shared name would change
the guest's key).

### What is rendered (`lib/ansible.nix`)

```json
"ansible_playbook": {
  "router": {
    "name": "vyos",
    "playbook": "/nix/store/<hash>-router/site.yml",
    "replayable": true,
    "groups": ["routers"],
    "extra_vars": {
      "ansible_host": "192.0.2.1", "ansible_user": "vyos",
      "ansible_connection": "ansible.netcommon.network_cli", "ansible_network_os": "vyos.vyos.vyos",
      "ansible_ssh_common_args": "-o StrictHostKeyChecking=accept-new",
      "vyos_password_file": "/run/secrets/vyos-admin"
    },
    "depends_on": ["proxmox_virtual_environment_vm.vyos"]
  }
}
```

and `terraform.required_providers.ansible = { source = "ansible/ansible";
version = "1.5.0"; }`. `depends_on` is there when the guest is managed (an
adopted guest is not rendered, so its playbook waits for nothing). In the
Pulumi program (`lib.toPulumi`) the resource is
`ansible:index/playbook:Playbook` with `options.dependsOn = ["${vyos}"]` and
`options.replaceOnChanges = ["*"]`, and the package
`ansible: { source: terraform-provider, version: 1.4.0, parameters: [ansible/ansible, 1.5.0] }`.

The provider writes a one-host inventory (`name`, in `groups`) and runs
`ansible-playbook -e hostname=<name> -e <var>='<value>' ... <playbook>`.

### When a playbook runs

- **Created**: the first deploy after it is declared (after its guest).
- **Changed**: any change to what is declared (the playbook's store path,
  that is its content; a variable; the guest's address) replaces the resource,
  and the replacement runs the new playbook. A replacement, not an update: the
  provider's update runs the arguments its create recorded (`args`), so an
  update would run the old playbook with the old variables (seen: below).
  `lib/pulumi.nix` sets `replaceOnChanges = ["*"]` on this type for that
  reason.
- **Unchanged**: a deploy that changes nothing declared does not run it.
- **Again, on demand**: a deploy with `--refresh` (`refresh` in the API)
  runs every playbook again. `replayable = true` makes the provider's read
  drop the resource from state, so the up creates it. A refresh also reads
  every other resource of the stack back from the provider.

A guest that a deploy replaces keeps its address, so its playbook's inputs do
not change and it does not run again by itself: deploy with `--refresh` after
replacing a guest. A playbook should wait for its guest to answer
(`ansible.builtin.wait_for_connection`): the guest's resource is created when
Proxmox says so, not when SSH is up.

### What a deploy reports

The runner (`cli/fleetkit_cli/ansible.py`) reads, for each playbook resource
of an `up`, whether its step ran the playbook (a create or a replacement) and
the counts of the `PLAY RECAP` of the output the provider keeps
(`ansiblePlaybookStdout`):

```json
{"stage": "infra", "kind": "ansible", "stack": "lab-guests", "key": "router",
 "ran": true, "changed": true,
 "recap": {"vyos": {"ok": 4, "changed": 1, "unreachable": 0, "failed": 0, "skipped": 0, "rescued": 0, "ignored": 0}}}
```

`changed` is true when a host of the recap changed something; a playbook that
did not run is `"ran": false, "changed": false`. The same records are in the
deploy's result, `ansible.<stack>`. Only the counts are read: what a task
printed is not passed on. It is in the stack's state, though (the provider's
`ansiblePlaybookStdout`, not a secret to it): a task that handles a secret
needs `no_log: true`. A playbook that fails fails the `up`, with the
provider's error (Ansible's output) as the engine's.

### Secrets

A secret is never a value in the model, the program or the state: it is a
file on the runner (decrypted there, for example by sops-nix on the
runner's host), and the playbook gets its path as an extra variable
and reads it, `lookup('ansible.builtin.file', vyos_password_file)`. The
path must be absolute and outside the store. The provider's own vault options
(`vault_files`, `vault_password_file`) are not rendered.

### The playbook on the runner

A playbook is a Nix path or a derivation, so the deploy runs exactly what was
evaluated. The program names it as plain text (`builtins.toFile`, which writes
the program to the store, takes no reference to a build output), and the
stack carries it with its context instead: `stacks.<stack>.needs`, from
`fromModel`, and `realise`, what `nix build` makes of it (`<drv>^out` for a
derivation's output, the path for a source). The runner builds each before it
runs the program, as a GC root in the run's directory (`needs-<i>`;
`cli/fleetkit_cli/render.py`).

### Ansible and its collections on the runner

`packages.<system>.fleetkit` puts `packages.<system>.ansible` on its `PATH`,
ahead of anything the host has: ansible-core 2.21.1 and the community package
ansible 14.0.0 (with `vyos.vyos` 6.0.0, `ansible.netcommon` 8.5.2,
`ansible.utils` 6.0.2, and paramiko and ansible-pylibssh for `network_cli`),
all from the flake's nixpkgs, and sets `ANSIBLE_COLLECTIONS_PATH` to its
collections alone, so a collection a user of the host installed is not used
(a `collections/` directory beside a playbook still is). Nothing is installed
by `ansible-galaxy` at deploy time. It adds about 840 MiB to the runner's
closure, whether an estate switches the add-on on or not.

`providers/ansible/collections.json` lists the collections it has, with
their versions; a playbook's `collections` are checked against it.
`tests/gen_ansible_collections.py` regenerates it (it builds the package),
and `tests/addons.sh` fails when its versions are not the ones the flake's
nixpkgs pins.

The runner's `ssh` is the host's, as for Colmena. Host keys: a guest the
deploy has just created is not in the runner's `known_hosts`; set
`ansible_ssh_common_args = "-o StrictHostKeyChecking=accept-new"` (or the
connection plugin's own setting) in `extraVars`, or put the key there first.

### Why the bridged provider

The `ansible/ansible` provider fits how fleetkit runs Pulumi: it bridges
like the other providers (`pulumi package get-schema
terraform-provider@1.4.0 ansible/ansible 1.5.0`; every Terraform name of it
is matched, `tests/gen_pulumi_names.py`), it runs `ansible-playbook` on the
runner, where the playbook is (above), and it keeps the output, from which
the run reports changed or unchanged. What does not fit is its update (it
replays recorded arguments) and that it re-runs only after a refresh; both
are handled above. A runner of fleetkit's own (an `ansible-playbook` stage
after `pulumi up`, as Colmena is) would run every deploy and could preview
with `--check`, but would sit outside the Pulumi plan, the update plan the
`up` is bound to and the state; it is not needed for what the issue asks.

### Verified

With Pulumi 3.247, the bridge 1.4.0 and `ansible/ansible` 1.5.0, a file
backend and a playbook against `localhost` (connection `local`; no guest):
the first `up` creates the resource and runs the playbook; a second `up`
leaves it unchanged; a changed playbook path is a replacement that runs the
new playbook (`preview --save-plan` and `up --plan` too); `up --refresh` runs
it again; the recap is in `ansiblePlaybookStdout`. Without
`replaceOnChanges` the same change is an update that ran the old playbook
(the file it writes kept the old content, the recap said `changed=0`).

Checked offline in the gates: `tests/terraform.sh` and `tests/pulumi.sh`
render the fixture estate `addon` (a playbook directory against a managed
VM, a playbook file against an adopted container) against the pinned
schemas; `tests/addons.sh` (above); `cli/tests/test_ansible.py` (the fake
engine: the report of a run that changed, did not change, did not run, and
a replacement; `realise` built before anything runs).

Not verified: a playbook against a real guest (a VyOS VM) through a real
deploy, and `network_cli` from the runner.
