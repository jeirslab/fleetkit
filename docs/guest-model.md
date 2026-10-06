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
  (`prevent_destroy`, `ignore_changes`), `companions.lxc_extra_conf` the raw
  lxc.conf lines. This is what the parity test compares with the legacy
  terraform baselines and what the slice-3 renderer will emit.
- `fleet.report.guestOptionPaths`: every guest option path (`*` = list
  element); the fidelity test requires a map line for each.
- `fleet.report.ipCollisions` / `vmidCollisions`: unchanged semantics, now
  fed by the derived `ipv4`.

`providerView` uses a recursive JSON value type. It is read-only and never
set by config, so it is not a loose block (nothing a user writes is
untyped).

## Kind discrimination

`kind = "lxc"` renders `proxmox_virtual_environment_container`, `kind =
"vm"` renders `proxmox_virtual_environment_vm`. One option tree serves both;
fields that exist for one kind only are rejected on the other with
`<path>: is lxc-only but kind is "vm"` (or the reverse):

- lxc only: `unprivileged`, `features.*`, `console.type`, `memory.swapMiB`,
  `environment`, `source.template`, `initialization.hostname`,
  `lxcExtraConf`, `devicePassthrough`, disk role `mount`, disk fields
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
   startOnBoot started startup cpu memory features unprivileged console
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
`mkDefault pool`). Opting a single guest out of the estate pool is not
supported yet (no guest needs it). The pool must belong to the guest's site
provider.

## References validated (config.assertions)

`on` (node; its site must have a proxmox provider), every
`networkInterfaces.*.network` (a network of that site), `bridge` (a bridge
of that site, carrying that network, available on the node), static
`address` inside the network cidr, unique interface names, at most one
static address per network, `disks` (exactly one root; datastore a storage
of the site, available on the node, holding `rootdir` (lxc) / `images`
(vm)), `source.template` (image of the site, content `vztmpl`),
`source.cdrom.image` (content `iso`), `source.clone.{datastore,node}`,
`vm.cloudInit.datastore`, `pool` / `effectivePool` (pool id of the site
provider), `hostpci.*.pcie` and `devicePassthrough.*.pcie` (pcie device on
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

## Deliberately NOT modelled yet (and why)

Never set by any baseline resource and not needed by any known guest; add
them (with a map line) when a guest needs one:

- container: `idmap` (one dev box's idmap is raw `lxc.idmap` lines in
  `lxcExtraConf`, which is what the legacy companion applies), `wait_for_ip`,
  `hook_script_file_id`, `template`, `purge_on_destroy`,
  `delete_unreferenced_disks_on_destroy`, `timeout_*`, `console.enabled`,
  `console.tty_count`, `features.mknod`, `network_interface.{enabled,host_managed}`,
  `disk.{acl,replicate}`, `mount_point.{acl,replicate}`, `initialization.entrypoint`,
  `initialization.dns.server` (deprecated singular), `initialization.ip_config.ipv6`
  (no guest has IPv6 config), `initialization.user_account.password`
  (secrets never live in the model).
- vm: everything outside `llm-1`'s 40 paths and the list above:
  `efi_disk`, `tpm_state`, `numa`, `usb`, `vga`, `audio_device`, `rng`,
  `smbios`, `amd_sev`, `virtiofs`, `watchdog`, `boot_order`, `acpi`,
  `hotplug`, `kvm_arguments`, `keyboard_layout`, `scsi_hardware`,
  `tablet_device`, `migrate`, `reboot*`, `stop_on_destroy`, `timeout_*`,
  `cpu.{affinity,flags,hotplugged,numa,sockets}`, `memory.{hugepages,keep_hugepages,shared}`,
  `disk.{aio,file_id,import_from,path_in_datastore,queues,serial,speed,replicate}`,
  `agent.{timeout,trim,type,wait_for_ip}`, `clone.retries`, `hostpci.{mapping,mdev,rom_file}`,
  `initialization.{interface,file_format,upgrade,*_data_file_id}`,
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
