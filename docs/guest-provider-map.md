# Guest model -> bpg/proxmox 0.115.0 provider map

Every option path of a guest (`fleet.guests.<estate>.<guest>.<path>`, as
listed by `nix eval .#fleet.report.guestOptionPaths`; `*` = list element)
maps to the provider argument(s) it feeds. `tests/guest_fidelity.py --gate`
reads the lines below and checks them against
`providers/schemas/bpg-proxmox-0.115.0.schema.json`.

## Line format (machine-readable)

One mapping per line, anywhere in this file outside this section's prose:

    <kind>: <model path> => <target>

| kind        | target is                                                                 | checked how |
| ----------- | ------------------------------------------------------------------------- | ----------- |
| `lxc`       | argument path in `proxmox_virtual_environment_container`                  | path must exist in the schema |
| `vm`        | argument path in `proxmox_virtual_environment_vm`                         | path must exist in the schema |
| `both`      | argument path that exists in BOTH resources                               | path must exist in both |
| `meta`      | Terraform meta-argument (`lifecycle.prevent_destroy`, `lifecycle.ignore_changes`) | must be one of those two |
| `companion` | a non-provider companion resource (`terraform_data.<guest>-lxc-conf`)     | must start with `terraform_data.` |
| `none`      | free text: why the field feeds no provider argument                        | must be non-empty |

Provider paths are dotted block/attribute names with list indexes dropped
(`initialization.ip_config.ipv4.address`). `network_device` on the vm is a
list-of-object ATTRIBUTE in the schema, not a block; its member keys are
addressed the same way (`network_device.bridge`). A model path may have
several lines (one per kind or per target block). Every path in
`fleet.report.guestOptionPaths` needs at least one line.

Provider arguments derived without a model option of their own: vm `name`
(= the guest name) and lxc `initialization.hostname` defaulting to the guest
name.

`lifecycle.ignore_changes` holds `ignoreChanges` plus what the kit adds by
itself: `clone` for a guest with `source.clone`, `description` for a
container with `lxcExtraConf` (the two extra `meta` lines below; the reasons
are in `guest-model.md`, "Adopting existing guests").

Not a guest option, so not in the list: the pool's comment,
`fleet.estates.<estate>.pools.proxmox.<pool>.comment` =>
`proxmox_virtual_environment_pool.comment` (rendered when set).

## Mappings

```text
both: vmid => vm_id
both: on => node_name
none: hostKeys.age => the guest's age recipient (age1...); feeds the estate's sops recipients (fleet.report.sops) and wins over the ssh key, not a provider argument
none: hostKeys.ed25519 => the guest's ssh host public key; feeds the estate's sops recipients (fleet.report.sops), not a provider argument
none: kind => selects the resource type: lxc = proxmox_virtual_environment_container, vm = proxmox_virtual_environment_vm
none: id => model identity "<estate>/<name>", read-only
none: mode => model lifecycle state (managed | adopted | planned); not rendered
none: nixos.module => the NixOS module of the guest, a file in the declaring repo; consumed by the deploy layer, not rendered
none: substrate => model role tags, checked against fleet.estates.<estate>.substrate
both: description => description
both: tags => tags
both: pool => pool_id
none: pool => the value "none" renders no pool_id: the guest is in no pool
both: effectivePool => pool_id
meta: protect => lifecycle.prevent_destroy
both: protection => protection
meta: ignoreChanges => lifecycle.ignore_changes
lxc: startOnBoot => start_on_boot
vm: startOnBoot => on_boot
both: started => started
both: startup.order => startup.order
both: startup.upDelay => startup.up_delay
both: startup.downDelay => startup.down_delay
both: cpu.cores => cpu.cores
both: cpu.units => cpu.units
both: cpu.limit => cpu.limit
both: cpu.architecture => cpu.architecture
vm: cpu.type => cpu.type
both: memory.dedicatedMiB => memory.dedicated
lxc: memory.swapMiB => memory.swap
vm: memory.floatingMiB => memory.floating
lxc: features.nesting => features.nesting
lxc: features.keyctl => features.keyctl
lxc: features.fuse => features.fuse
lxc: features.mount => features.mount
lxc: unprivileged => unprivileged
lxc: console.type => console.type
lxc: console.omit => console
lxc: environment => environment_variables
lxc: defaultDatastore => disk.datastore_id
lxc: defaultDatastore => mount_point.volume
vm: defaultDatastore => disk.datastore_id
vm: defaultDatastore => efi_disk.datastore_id
lxc: initialization.hostname => initialization.hostname
both: initialization.dns.servers => initialization.dns.servers
both: initialization.dns.domain => initialization.dns.domain
both: initialization.userAccount.keys => initialization.user_account.keys
vm: initialization.userAccount.username => initialization.user_account.username
both: source.osType => operating_system.type
lxc: source.template => operating_system.template_file_id
both: source.clone.vmid => clone.vm_id
both: source.clone.datastore => clone.datastore_id
both: source.clone.full => clone.full
both: source.clone.node => clone.node_name
vm: source.cdrom.image => cdrom.file_id
vm: source.cdrom.interface => cdrom.interface
meta: source.clone.vmid => lifecycle.ignore_changes
vm: vm.agent => agent.enabled
vm: vm.bios => bios
vm: vm.machine => machine
vm: vm.serialDevices => serial_device.device
vm: vm.cloudInit.datastore => initialization.datastore_id
vm: vm.cloudInit.type => initialization.type
vm: vm.cloudInit.interface => initialization.interface
vm: vm.cloudInit.upgrade => initialization.upgrade
vm: vm.scsiHardware => scsi_hardware
vm: vm.efiDisk.datastore => efi_disk.datastore_id
vm: vm.efiDisk.fileFormat => efi_disk.file_format
vm: vm.efiDisk.type => efi_disk.type
vm: vm.efiDisk.preEnrolledKeys => efi_disk.pre_enrolled_keys
lxc: networkInterfaces.*.name => network_interface.name
none: networkInterfaces.*.network => site network NAME: keys ipv4.<network> and supplies the prefix and gateway defaults (the vm network_device has no name either)
lxc: networkInterfaces.*.bridge => network_interface.bridge
vm: networkInterfaces.*.bridge => network_device.bridge
lxc: networkInterfaces.*.vlanId => network_interface.vlan_id
vm: networkInterfaces.*.vlanId => network_device.vlan_id
lxc: networkInterfaces.*.macAddress => network_interface.mac_address
vm: networkInterfaces.*.macAddress => network_device.mac_address
lxc: networkInterfaces.*.firewall => network_interface.firewall
vm: networkInterfaces.*.firewall => network_device.firewall
lxc: networkInterfaces.*.rateLimit => network_interface.rate_limit
vm: networkInterfaces.*.rateLimit => network_device.rate_limit
lxc: networkInterfaces.*.mtu => network_interface.mtu
vm: networkInterfaces.*.mtu => network_device.mtu
vm: networkInterfaces.*.model => network_device.model
both: networkInterfaces.*.address => initialization.ip_config.ipv4.address
both: networkInterfaces.*.prefix => initialization.ip_config.ipv4.address
both: networkInterfaces.*.gateway => initialization.ip_config.ipv4.gateway
both: ipv4 => initialization.ip_config.ipv4.address
none: disks.*.role => selects the block: lxc root = disk, lxc mount = mount_point, vm root and data = disk entries (root first)
lxc: disks.*.datastore => disk.datastore_id
lxc: disks.*.datastore => mount_point.volume
vm: disks.*.datastore => disk.datastore_id
lxc: disks.*.size => disk.size
lxc: disks.*.size => mount_point.size
vm: disks.*.size => disk.size
lxc: disks.*.path => mount_point.path
lxc: disks.*.backup => mount_point.backup
vm: disks.*.backup => disk.backup
lxc: disks.*.readOnly => mount_point.read_only
lxc: disks.*.quota => disk.quota
lxc: disks.*.quota => mount_point.quota
lxc: disks.*.shared => mount_point.shared
lxc: disks.*.mountOptions => disk.mount_options
lxc: disks.*.mountOptions => mount_point.mount_options
vm: disks.*.interface => disk.interface
vm: disks.*.discard => disk.discard
vm: disks.*.iothread => disk.iothread
vm: disks.*.ssd => disk.ssd
vm: disks.*.fileFormat => disk.file_format
vm: disks.*.cache => disk.cache
lxc: devicePassthrough.*.path => device_passthrough.path
none: devicePassthrough.*.pcie => provenance only: the node pcie device the device node belongs to (validated to be on the guest's node)
lxc: devicePassthrough.*.uid => device_passthrough.uid
lxc: devicePassthrough.*.gid => device_passthrough.gid
lxc: devicePassthrough.*.mode => device_passthrough.mode
lxc: devicePassthrough.*.denyWrite => device_passthrough.deny_write
vm: hostpci.*.device => hostpci.device
vm: hostpci.*.pcie => hostpci.id
vm: hostpci.*.pcieExpress => hostpci.pcie
vm: hostpci.*.rombar => hostpci.rombar
vm: hostpci.*.xvga => hostpci.xvga
lxc: idmap.*.type => idmap.type
lxc: idmap.*.containerId => idmap.container_id
lxc: idmap.*.hostId => idmap.host_id
lxc: idmap.*.size => idmap.size
meta: lxcExtraConf => lifecycle.ignore_changes
companion: lxcExtraConf => terraform_data.<guest>-lxc-conf (local-exec over root ssh: rewrites the "# BEGIN/END fleetkit lxc_extra_conf" block of /etc/pve/lxc/<vmid>.conf, reboots the CT if running)
```
