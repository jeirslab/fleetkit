# Guest model (provider-shaped)

`modules/guests.nix` models every Proxmox guest after the bpg/proxmox
0.115.0 resource it renders to. The field-by-field map is
`docs/guest-provider-map.md` (checked by `tests/guest_fidelity.py`); this
file records the decisions.

Sources: provider schema `providers/schemas/bpg-proxmox-0.115.0.schema.json`
(flattened in `providers/index/proxmox_virtual_environment_{container,vm,pool}.txt`),
usage counts from the legacy terraform baselines (homelab@omo-plugin: 44
containers + 1 vm; xgcs@unstable: 27 containers, 0 vm), layering from the
legacy fleetkit model.

## Shape

```nix
fleet.guests.<estate>.<name> = {
  vmid = 140; kind = "lxc"; on = hn.pve1.id;          # identity
  mode = "managed"; substrate = [ ];                  # model-only
  networkInterfaces = [ { network = "lan"; address = "10.0.0.73"; } ];
  disks = [
    { role = "root"; size = 32; }                      # datastore = defaultDatastore
    { role = "mount"; path = "/data"; size = "500G"; backup = false; }
    { role = "mount"; path = "/data/cold"; size = "2T"; datastore = st.xgcs.id; backup = false; }
  ];
  cpu.cores = 6; memory.dedicatedMiB = 16384;
  startup.order = 3; protect = true;
  tags = [ ... ]; description = "...";
};
```

The options for declaring a guest that already exists exactly as it is (id
mapping, no pool, no console block, scsi hardware, EFI disk, cloud-init drive)
are in "Adopting existing guests" below.

Read-only, derived per guest: `id`, `ipv4.<network>` (static addresses of
`networkInterfaces`, keyed by network name; `config.nix` reads
`guests.homelab.caddy.ipv4.lan`) and `effectivePool`.

Read-only, derived fleet-wide:

- `fleet.report.providerView.<estate>.<guest>` = `{ resource; args;
  lifecycle; companions; }`: the effective guest after layering, in the
  provider's argument names. Single blocks are objects (`cpu`, `memory`,
  lxc `disk`, `features`, `initialization`, ...), repeatable blocks are
  lists (`network_interface`, `mount_point`, `ip_config`, vm `disk`,
  `network_device`, `hostpci`, `serial_device`); null leaves and empty
  blocks are dropped; lxc `mount_point` is always present (the baselines
  render `[]`). `lifecycle` holds the Terraform meta-arguments
  (`prevent_destroy`, `ignore_changes`; the latter also holds what the kit
  ignores by itself, `clone`, `description` and what an import does not
  record, see "Adopting existing guests"), `companions.lxc_extra_conf` the raw lxc.conf lines. This is what the parity test compares with the legacy
  terraform baselines and what the slice-3 renderer will emit.
- `fleet.report.guestOptionPaths`: every guest option path (`*` = list
  element); the fidelity test requires a map line for each.
- `fleet.report.ipCollisions` / `vmidCollisions`: unchanged semantics, now
  fed by the derived `ipv4`.
- `fleet.report.lxcExtraConfIdmap`: per guest id, the `lxc.idmap` lines its
  `lxcExtraConf` carries (declare them as `idmap` instead).
- `fleet.report.unrecordedIgnored`: per guest id with `adoption.unrecorded =
  "ignore"`, the provider arguments that puts under `ignore_changes`: what
  the engine does not apply to that guest ("Adoption without a reboot").

`providerView` uses a recursive JSON value type. It is read-only and never
set by config, so it is not a loose block (nothing a user writes is
untyped).

## Kind discrimination

`kind = "lxc"` renders `proxmox_virtual_environment_container`, `kind =
"vm"` renders `proxmox_virtual_environment_vm`. One option tree serves both;
fields that exist for one kind only are rejected on the other with
`<path>: is lxc-only but kind is "vm"` (or the reverse):

- lxc only: `unprivileged`, `features.*`, `console.type`, `console.omit`,
  `memory.swapMiB`, `environment`, `source.template`,
  `initialization.hostname`, `lxcExtraConf`, `idmap`, `devicePassthrough`,
  disk role `mount`, disk fields
  `path readOnly quota shared mountOptions`.
- vm only: `cpu.type`, `memory.floatingMiB`, `vm.*`, `source.cdrom`,
  `initialization.userAccount.username`,
  `hostpci`, `networkInterfaces.*.model`, disk role `data`, disk fields
  `interface discard iothread ssd fileFormat cache`.

Only the guest's OWN definitions are checked. Estate `guestDefaults` may set
knobs of either kind; each guest silently takes only the knobs of its kind.

## Layering

Lowest to highest; `null` is silent at every layer; attrsets merge per key;
scalars and lists replace (a guest's `tags` replaces the estate's, it does
not extend them).

1. Schema defaults (lxc only): `unprivileged = true`, `features.nesting = true`
   (the legacy fleetkit defaults; legacy fuse/keyctl = true defaults are NOT
   carried: the legacy emitter only wrote them when true).
2. Site provider defaults, `fleet.sites.<site>.providers.proxmox.defaults`
   of the guest's node's site: `startOnBoot`, `started` (both kinds),
   `console.type`, `source.osType`, the template volume (lxc), and
   `nic.name` / `nic.bridge` for interface 0 / every interface's bridge.
3. Estate defaults, `fleet.estates.<estate>.guestDefaults`: the same knob
   set as a guest (`description tags pool protect protection ignoreChanges
   adoption startOnBoot started startup cpu memory features unprivileged console
   environment defaultDatastore initialization source vm`).
4. The guest.

Interface defaults: `name` = provider `nic.name` for interface 0, `eth<i>`
after; `bridge` = provider `nic.bridge`; `prefix` = the network's cidr
prefix; `gateway` = the address of the network's gateway router, on
interface 0 only (one default route).

Disk defaults: a disk whose `datastore` is null uses the layered
`defaultDatastore` (replaces legacy `defaultDatastore`; the legacy
"local-storage" default is deliberately not carried, it is not a real
homelab datastore).

DNS: `initialization.dns` comes only from the layers (estate guestDefaults
in practice), never from the network: homelab guests use
`[caddy, 1.1.1.1]` + domain `lab.lan`, xgcs guests use
`[1.1.1.1, 9.9.9.9]` and no domain, on the same network.

Pool: `pool` (guest, else guestDefaults) else the estate's
`placement.pool`, published as `effectivePool` and rendered as `pool_id` =
the pool's name. Every XG guest therefore shows `effectivePool =
"xgcs/proxmox/xgcs"` without repeating it (legacy: XG `hosts/default.nix`
`mkDefault pool`). `pool = "none"` takes a guest out of the estate pool: no
`pool_id` is rendered (see "Adopting existing guests"). The pool must belong
to the guest's site provider.

## References validated (config.assertions)

`on` (node; its site must have a proxmox provider), every
`networkInterfaces.*.network` (a network of that site), `bridge` (a bridge
of that site, carrying that network, available on the node), static
`address` inside the network cidr, unique interface names, at most one
static address per network, `disks` (exactly one root; datastore a storage
of the site, available on the node, holding `rootdir` (lxc) / `images`
(vm)), `source.template` (image of the site, content `vztmpl`),
`source.cdrom.image` (content `iso`), `source.clone.{datastore,node}`,
`vm.cloudInit.datastore`, `vm.efiDisk.datastore` (as a disk's), `pool` /
`effectivePool` (pool id of the site provider, or `"none"`), `hostpci.*.pcie` and `devicePassthrough.*.pcie` (pcie device on
the guest's node; hostpci needs a pci address),
`initialization.userAccount.keys` (`<principal>/ssh/<label>` resolving to
a principal public key; resolved to the key text in providerView),
estate key, substrate both ways, vmid per cluster (allowlist), and the
references inside every estate's `guestDefaults`.

## Modelled provider arguments

Everything the baselines set (counts: containers xgcs/27, homelab/44):
`vm_id node_name description tags` (all), `pool_id` (27/0),
`start_on_boot started` (all), `startup.order` (27/0),
`unprivileged features.nesting` (all), `features.keyctl` (0/8),
`features.fuse` (0/2), `console.type` (all), `cpu.cores` (all),
`memory.dedicated memory.swap` (all), `disk.datastore_id disk.size` (all),
`mount_point.volume path size backup` (9 mounts on 8 / 4 on 4),
`network_interface.name bridge` (all), `initialization.hostname
dns.servers ip_config.ipv4.address gateway` (all), `dns.domain` (0/44),
`operating_system.template_file_id type` (all); the one vm (`llm-1`):
`agent.enabled bios machine clone.{vm_id,datastore_id,full} cpu.{cores,type}
memory.dedicated disk.{interface,datastore_id,size} initialization.{datastore_id,type,dns,ip_config,user_account.keys}
network_device.bridge operating_system.type serial_device.device name started`.
Terraform meta-arguments: `lifecycle.prevent_destroy` (9/24, as `protect`)
and `lifecycle.ignore_changes` (all, as `ignoreChanges`). The lxc extra
config companion (`terraform_data.<guest>-lxc-conf`: one xgcs dev box,
homelab arr, arr-dl, media-server-next) as `lxcExtraConf`.

Obvious provider arguments the baselines never set but that are modelled
because the owner asked for them or they are cheap and unambiguous:
`protection`, `startup.up_delay/down_delay`, `cpu.units/limit/architecture`,
`memory.floating`, `features.mount`, `environment_variables`,
`network_interface.{vlan_id,mac_address,firewall,rate_limit,mtu}` /
`network_device.{model,...}`, `mount_point.{read_only,quota,shared,mount_options}`,
`disk.{quota,mount_options}` (lxc), vm `disk.{discard,iothread,ssd,file_format,cache,backup}`,
`device_passthrough.*`, `hostpci.*`, `clone.node_name`, `cdrom.*`.

## Adopting existing guests

An existing guest is imported into state and then compared with the render. A
difference is an update in place (a container is rebooted for most of them)
or a replacement (the guest is destroyed). The options below exist so that a
live guest can be declared as it is. Each is optional, and unset it renders
nothing: a model that sets none of them renders what it rendered before them,
with one exception (the description of a container with `lxcExtraConf`,
below). What the provider does is from its source at tag v0.115.0
(`proxmoxtf/resource/container/container.go`, `proxmoxtf/resource/vm/vm.go`,
`proxmoxtf/resource/pool/pool.go`, `proxmox/nodes/containers/containers_types.go`);
none of it was run against a cluster here, except where "Adoption without a
reboot" says what was seen.

```nix
fleet.estates.<estate>.pools.proxmox.<pool>.comment = "guests of the lab";

fleet.guests.<estate>.<ct> = {
  pool = "none";                      # in no pool: no pool_id
  console.omit = true;                # no console block
  idmap = [                           # lxc.idmap lines, in file order
    { type = "uid"; containerId = 0;    hostId = 100000; size = 1000; }
    { type = "uid"; containerId = 1000; hostId = 1000;   size = 1; }
    { type = "uid"; containerId = 1001; hostId = 101001; size = 64535; }
    { type = "gid"; containerId = 0;    hostId = 100000; size = 1000; }
    { type = "gid"; containerId = 1000; hostId = 1000;   size = 1; }
    { type = "gid"; containerId = 1001; hostId = 101001; size = 64535; }
  ];
};

fleet.guests.<estate>.<vm>.vm = {
  scsiHardware = "virtio-scsi-single";
  efiDisk = { datastore = "<site>/<storage>"; fileFormat = "raw"; type = "4m"; preEnrolledKeys = true; };
  cloudInit = { interface = "ide2"; upgrade = true; };
};

# what an import does not record: ignored, for every guest of the estate ...
fleet.estates.<estate>.guestDefaults.adoption.unrecorded = "ignore";
# ... and applied again for one guest (a reboot, once)
fleet.guests.<estate>.<guest>.adoption.unrecorded = "apply";
```

### Adoption without a reboot (`adoption.unrecorded`)

The options above make a declaration equal to the live guest. Some arguments
cannot be made equal: the provider's import does not record them, so state
has nothing where the declaration has a value, and the adoption carries an
update in place for them **even when the live values already match**. The
provider reboots a container to apply it, and stops and starts a running VM.

`adoption.unrecorded` is a guest knob, layered like the others (a guest's own
value over `guestDefaults`; `null` is silent):

| value | |
|---|---|
| `"apply"` | the effective default, and what an estate that sets nothing gets: nothing is rendered for it. The adoption carries the update and the reboot (`fleetkit adopt`: `import+unrecorded`, `--accept-update`) |
| `"ignore"` | the arguments below are added to the guest's `lifecycle.ignore_changes` (Pulumi: `options.ignoreChanges`), after whatever `ignoreChanges` and the kit already put there, none twice. They are still rendered, and still used when the guest is created from scratch |

| kind | ignored | from |
|---|---|---|
| container | `cpu`, `memory`, `vm_id`, `console` | seen on a real container (2026-10-08, [issue 61]) |
| vm | `cpu`, `memory`, `scsi_hardware`, `agent`, `operating_system`, `efi_disk` | the provider source, **not observed** |

**The cost.** While a guest is under `"ignore"`, the engine does not apply a
change to those arguments on it: more cores or memory, another console, a
VM's controller, agent, OS type or EFI disk in the model change nothing on the
guest, and its preview says `same`. To apply one, set `adoption.unrecorded =
"apply"` on that guest and deploy: the engine then sees every one of those
arguments as new (state never had them) and the provider **reboots the guest
once** (a container; a running VM is stopped and started), whatever changed.
That is a deliberate step, named in the model. State has them from then on,
so leave that guest on `"apply"`: back on `"ignore"` it would only stop being
managed again. `fleet.report.unrecordedIgnored` lists, per guest id, the
arguments ignored this way: what the engine is not managing.

**Container: what was seen.** A container was imported with the bridged
provider; the import had no `cpu`, `memory` or `vmId`, and a `console` only
when it is not the provider's default, so the adoption planned an update for
them with every value matching, and the provider rebooted the container. With
those four (and `operatingSystem`, `initialization`, which that estate ignores
itself) under `ignoreChanges` in the real program, `fleetkit adopt --apply
--accept-update <key>` left one difference, `timeoutStart`, which the provider
applied with no task on the node and no reboot; the next preview, also with
`--refresh`, was all `same`. `fleetkit adopt` now applies such a difference
without `--accept-update` (`import+local`, `pulumi.md`).

What the source says about it (`container.go`, `containerRead`): `cpu`,
`memory` and `console` are set only when state already has the block or a live
value differs from the provider's default (1 core, no limit, no units,
`amd64`; 512 MiB and no swap; enabled, `tty`, 2 ttys), and `vm_id` only to
repair a legacy `-1`. So a container whose cpu or memory is not the default
may have that block recorded by its import; the ignore is then not needed for
it, and costs the same. `containerUpdate` sets `rebootRequired` for any
change of `console`, `cpu` or `memory`.

**VM: what the source says** (`proxmoxtf/resource/vm/vm.go` at v0.115.0,
`vmReadCustom`, which `vmRead` and so the import call; an import starts with
the id and `node_name` only, and no `clone` in state):

- `cpu`: set only when state has the block or a live value differs from the
  provider's default (1 core, 1 socket, type `qemu64`, no architecture, flags,
  hotplugged vcpus, limit or units);
- `memory`: likewise (512 MiB dedicated, no floating or shared memory, no
  hugepages);
- `agent`: likewise (not enabled, no trim, type `virtio`), so a VM with the
  agent off has none after an import and a declared `vm.agent = false` is a
  block added;
- `operating_system`: an empty list when the type is the provider's default
  `other`;
- `efi_disk`: set only when it differs from the default (`local-lvm`, `raw`,
  `2m`, no pre-enrolled keys);
- `scsi_hardware`: set only when the VM's config has a `scsihw` line. Without
  one the provider's default `virtio-scsi-pci` is a difference whether the
  model declares a controller or not, and applying it is a real change of
  controller (Proxmox's own default is `lsi`);
- `vm_id` is always set there ("during import these core attributes might not
  be set"), unlike the container's, so it is not in the VM's list; all eight
  `timeout_*` arguments are filled in with their defaults, so a VM has no
  `timeoutStart` left over.

`vmUpdate` sets `rebootRequired` for a change of each of the six (for `cpu`
unless only the hotplugged vcpus change and the VM hot-plugs cpu, for `memory`
unless it only grows and the VM hot-plugs memory). A running VM is then
stopped and started (`vmPowerOffForPendingChanges`, then
`vmFinalizePowerState`), unless `reboot_after_update` is false, which the kit
does not render. The one VM adoption that was run fits this: a
stopped VM, adopted with an update for `cpu`, `memory` and `scsiHardware`, was
not started. A running VM was not tried.

Arguments the kit does not render (`vga`, `watchdog`, `rng`, ...) are read the
same way; nothing is declared for them, so they are not in the list.

### Container id mapping (`idmap`)

`idmap` renders the provider's `idmap` blocks (`type`, `container_id`,
`host_id`, `size`; all four required, so an entry without one fails
evaluation). `type` is `uid` or `gid`, where the conf file says `u` or `g`.

The provider treats every `lxc.idmap:` line of `/etc/pve/lxc/<vmid>.conf` as
this argument, wherever the line is:

- read: `CustomLXCConfig.UnmarshalJSON` parses the `lxc` list of the config
  the API returns and keeps the `lxc.idmap` entries; `containerRead` always
  sets `idmap` from them, in file order (the argument is a list: declare the
  entries in the order the file has them);
- write: `containerSetIDMaps` runs over ssh on the node, deletes every line
  matching `^lxc\.idmap:` from the conf file and appends the declared ones at
  the end; `containerUpdate` then reboots a running container.

So `lxc.idmap` lines written as raw `lxcExtraConf` lines are the same thing
to the provider, and it does not know they were meant to be the companion's:
a guest that has them live and declares no `idmap` plans the removal of its
mapping (the lines deleted, inside the companion's marked block too, and a
reboot). Declare the mapping as `idmap`, and only there:

- `idmap` beside `lxc.idmap` lines in `lxcExtraConf` fails evaluation;
- `fleet.report.lxcExtraConfIdmap` lists, per guest id, the `lxc.idmap` lines
  still declared as raw lines (nothing is rendered from them).

Creating or changing an `idmap` needs the provider's ssh access to the node
(the API has no parameter for it). The render carries no provider `ssh`
block; the provider then takes it from its environment
(`PROXMOX_VE_SSH_USERNAME`, `PROXMOX_VE_SSH_AGENT`, ...). An adoption whose
declared mapping equals the live one changes nothing and needs no ssh.

### No pool (`pool = "none"`)

`null` is silent at every layer, so it cannot take a guest out of the estate's
`placement.pool`. `pool = "none"` (on a guest, or in `guestDefaults`) says the
guest is in no pool: `effectivePool` is null and no `pool_id` is rendered. A
guest's own pool overrides a `guestDefaults.pool = "none"` as usual.

The grant does not go with the pool: a guest with `pool = "none"` is still
held to the vmid and host ranges of the estate's `placement.pool` (a tenant
cannot leave its ranges by leaving its pool).

Why it matters: the container's `pool_id` is `ForceNew` and is only used at
create (`containerCreateCustom`, `containerCreateClone`); `containerRead`
never sets it. A rendered `pool_id` that state does not have is therefore a
replacement of the container. The vm's `pool_id` is read (`vmReadCustom`
sets it to the pool the VM is found in) and changed in place
(`vmUpdatePool`), and an omitted one is never a difference
(`DiffSuppressFunc`: new value empty).

### No console block (`console.omit`)

The site default (`providers.proxmox.defaults.console`) gives every container
a `console` block. `console.omit = true` renders none; setting `console.type`
on the same guest fails evaluation.

The provider's values for an omitted block are `enabled = true`,
`type = "tty"`, `tty_count = 2` (`dvConsoleEnabled`, `dvConsoleMode`,
`dvConsoleTTYCount`), which are also Proxmox's. `containerRead` records a
console block only when state already has one or the live values differ from
those three. A container on the default tty console therefore has no console
block after an import, and a rendered `console { type = "tty" }` is a block
added: an update, and `containerUpdate` reboots the container for any console
change. "Default tty" is said by omission (`console.omit = true`), not by
`console.type = "tty"`. `console.enabled` and `console.tty_count` are not
modelled: a container that differs from the default in those keeps a
difference.

### VM controller, EFI disk, cloud-init drive

- `vm.scsiHardware` -> `scsi_hardware`, one of `lsi`, `lsi53c810`,
  `virtio-scsi-pci`, `virtio-scsi-single`, `megasas`, `pvscsi`
  (`SCSIHardwareValidator`). Unset, nothing is rendered and the provider's
  default `virtio-scsi-pci` applies; a change is made in place with a reboot.
- `vm.efiDisk` -> `efi_disk` (`datastore_id`, `file_format`, `type`,
  `pre_enrolled_keys`). `datastore` null takes the layered `defaultDatastore`
  and is checked like a disk's (a storage of the site, on the node, holding
  `images`); with neither, evaluation fails, because the provider would
  silently use `local-lvm`. `type` is `2m` or `4m`; changing it on an existing
  VM forces a replacement (`forceNewOnEFIDiskTypeChange`), adding or removing
  the block does not.
- `vm.cloudInit.interface` -> `initialization.interface` (`ide0..3`,
  `sata0..5`, `scsi0..30`), `vm.cloudInit.upgrade` -> `initialization.upgrade`.
  The provider reads `upgrade` as true when Proxmox has no value, and sends it
  on an update only when it changed (only `root@pam` may set it).

### Pool comment

`fleet.estates.<estate>.pools.proxmox.<pool>.comment` -> the pool's `comment`.
The provider reads it and sends it on every update (`pool.go`, `poolUpdate`),
so an adopted pool whose comment is not declared has it cleared.

### `clone` is under `ignore_changes`

A guest with `source.clone` renders the `clone` block and gets `clone` in
`lifecycle.ignore_changes` (Pulumi: `options.ignoreChanges`), beside whatever
`ignoreChanges` it declares.

In both resources the `clone` block itself is not `ForceNew`, but every member
is (`vm_id` required and `ForceNew`; `datastore_id`, `node_name`, `full`, and
the vm's `retries`, `ForceNew` with defaults), and neither `vmRead` nor
`containerRead` ever sets `clone`: it is only what the create was told. An
imported guest has no `clone` in state, so a declared one is `vm_id` changing
from nothing to a number: a replacement. Ignored, the block is still used when
the guest is created from scratch (ignore applies to an existing resource) and
is never compared afterwards, which loses nothing: no change to `clone` can be
applied to an existing guest except by replacing it. The need came from VMs;
the container's block is the same in the source, so both kinds get it.

### `lxcExtraConf`: the description, and what is not rendered

How the companion was applied (legacy fleetkit, `nix/lib/tf/proxmox.nix`,
`mkLxcExtraConf`; this branch has no applier): a `terraform_data` with a
`local-exec` that, over root ssh on the node, rewrites the part of
`/etc/pve/lxc/<vmid>.conf` between the lines
`# BEGIN fleetkit lxc_extra_conf` and `# END fleetkit lxc_extra_conf`
(appending the block when there is none) and reboots a running container.

Why the markers reach the description: in a Proxmox guest conf file every
line starting with `#` IS the description (pve-container,
`src/PVE/LXC/Config.pm`, `parse_pct_config`: each `#` line is appended to
`description`; `write_pct_config` writes the description back as `#` lines at
the top of the file and the `lxc.*` entries after the other keys). So the API
returns `<declared>\n BEGIN fleetkit lxc_extra_conf\n END fleetkit
lxc_extra_conf\n`, `containerRead` stores that text as it is, and the
declared description (normalised by the schema's `StateFunc` to trimmed text
plus one newline) never equals it. The same rewrite is why, on a guest Proxmox
has rewritten since, the raw lines are found outside the marked block.

Choice: a container with a non-empty `lxcExtraConf` gets `description` in
`lifecycle.ignore_changes`. The alternative, rendering a description that
ends in the marker text, was rejected: it depends on the exact text Proxmox
returns, it would write marker lines into the conf file of a guest no
companion ever touched, and "fixing" the description the other way (an update
with the declared text) makes Proxmox drop the marker lines, after which the
legacy applier no longer finds its block and appends a second one. The cost:
a change to the declared description of such a guest is not applied. This is
the one render that differs for an unchanged model: such a guest gains
`ignore_changes = [ "description" ]`.

No lxc-conf companion is rendered at all, by `lib.internal.guests` or by the
Pulumi program compiled from it. For an existing guest that is harmless (the
lines are in its conf file and nothing rendered touches them, see below). A
guest with `lxcExtraConf` that Pulumi creates from scratch comes up WITHOUT
those lines (no bind mount, no apparmor profile, no device rule) until someone
writes them into the conf file by hand. Such guests are listed in
`locals.fleet_unrendered_companions`, which is the Pulumi program's
`outputs.fleet_unrendered_companions`.

### Raw `lxc.*` lines the model does not declare

A live conf file may hold raw lines outside the marked block (a tmpfs on
`/run`, `lxc.apparmor.profile: unconfined`, `lxc.cgroup2.devices.allow`, a
`/dev/net/tun` bind). Nothing has to be declared for them: the provider
neither reads nor writes any raw key except `lxc.idmap`. The API's `lxc` list
is parsed into `CustomLXCConfig` (`Raw`, `IDMaps`); `Raw` is never used,
`IDMaps` feeds `idmap`, and no create or update body has an `lxc` field (the
API takes none, which is why `idmap` goes over ssh, with a `sed` that matches
`lxc.idmap` lines only). They are not in state, never in a plan, and survive
every update the provider makes.

## Deliberately NOT modelled yet (and why)

Never set by any baseline resource and not needed by any known guest; add
them (with a map line) when a guest needs one:

- container: `wait_for_ip`,
  `hook_script_file_id`, `template`, `purge_on_destroy`,
  `delete_unreferenced_disks_on_destroy`, `timeout_*`, `console.enabled`,
  `console.tty_count`, `features.mknod`, `network_interface.{enabled,host_managed}`,
  `disk.{acl,replicate}`, `mount_point.{acl,replicate}`, `initialization.entrypoint`,
  `initialization.dns.server` (deprecated singular), `initialization.ip_config.ipv6`
  (no guest has IPv6 config), `initialization.user_account.password`
  (secrets never live in the model).
- vm: everything outside `llm-1`'s 40 paths and the list above:
  `tpm_state`, `numa`, `usb`, `vga`, `audio_device`, `rng`,
  `smbios`, `amd_sev`, `virtiofs`, `watchdog`, `boot_order`, `acpi`,
  `hotplug`, `kvm_arguments`, `keyboard_layout`,
  `tablet_device`, `migrate`, `reboot*`, `stop_on_destroy`, `timeout_*`,
  `cpu.{affinity,flags,hotplugged,numa,sockets}`, `memory.{hugepages,keep_hugepages,shared}`,
  `disk.{aio,file_id,import_from,path_in_datastore,queues,serial,speed,replicate}`,
  `agent.{timeout,trim,type,wait_for_ip}`, `clone.retries`, `hostpci.{mapping,mdev,rom_file}`,
  `initialization.{file_format,*_data_file_id}`,
  `network_device.{disconnected,enabled,queues,trunks}`, `mac_addresses`.
  One vm exists, so its use says little about generality; llm-1's GPU
  passthrough is only in `ignore_changes` (`hostpci` is never rendered).
- computed-only attributes (`id`, `ipv4`, `ipv6`, `ipv4_addresses`, ...).
- the provider alias (`proxmox.main` / `proxmox.gpu-ml`) is renderer
  plumbing (one provider per site provider), not a guest field.

There is no escape hatch for unmodelled arguments: an unknown option fails
with "does not exist". If one is ever needed it must be a loose block added
to `fleet.report.looseBlocks` and `docs/schema-todo.md`.

## sops and terranix (question from the owner)

Terranix renders Nix to `config.tf.json`; it does not decrypt anything. In
both legacy baselines the `carlpett/sops` provider (every stack) is used for
one thing only: `data.sops_file.secrets` feeding the `proxmox` and
`cloudflare` provider `api_token`. No resource consumes a sops value. The
model already names those credentials as references
(`sites.<s>.providers.proxmox.tokenRef`, `accounts.cloudflare.main.tokenRef`).
Recommendation for slice 3: do not render the sops provider; the fleet CLI
runs tofu under `sops exec-env` (or equivalent) mapping each tokenRef to the
provider's environment variable (`PROXMOX_VE_API_TOKEN`,
`CLOUDFLARE_API_TOKEN`). That keeps decrypted values out of the rendered JSON
and out of tofu state (data sources are persisted in state), and drops a
provider. Decision belongs to slice 3.

## Data provenance

`guests.nix` is in the new shape (rendered from the legacy host files and
terraform baselines, then integrated). homelab lxc `ignoreChanges` holds the
effective terraform list: the legacy emitter appended `operating_system` and
`initialization` to every lxc's host-level list, so most guests inherit the
estate `guestDefaults` value. Remaining differences against the legacy
baselines are declared in `tests/parity_known.json` (each with a reason and
exact values): four homelab `protect` values the legacy emitter forced from
stateful tags, llm-1's `hostpci` (never rendered by the legacy emitter) and
its explicit `firewall = false`, and four guests with no terraform resource
(homelab llm-cold; xgcs vpn, xg-caddy, xg-ntfy in the platform-edge stack the
baseline does not carry).

[issue 61]: https://github.com/jeirslab/fleetkit/issues/61
