"""Which resource types are guests. Data only, no imports: the gate that keeps
this list complete (tests/guest_types.py) loads the file by path, without the
package's dependencies.

A guest is what a replace or a delete destroys and an in-place update reboots:
every resource type of the bridged bpg/proxmox provider that is a container or
a virtual machine. `tests/guest_types.py` (a gate) and `cli/tests/test_guests.py`
derive every resource token that matches vm / container / lxc from the pinned
name map (providers/pulumi/names/bpg-proxmox-*.json) and fail when one is in
neither set below, so a provider bump that adds a guest type cannot fail open.
"""

GUEST_TYPES = frozenset({
    "proxmox:index/virtualEnvironmentContainer:VirtualEnvironmentContainer",
    "proxmox:index/virtualEnvironmentVm:VirtualEnvironmentVm",
    "proxmox:index/virtualEnvironmentVm2:VirtualEnvironmentVm2",
    "proxmox:index/vm:Vm",
    "proxmox:index/clonedVm:ClonedVm",
    "proxmox:index/virtualEnvironmentClonedVm:VirtualEnvironmentClonedVm",
})

# Tokens the same pattern matches that are not guests, each with why.
NOT_GUESTS = {
    "proxmox:index/storageLvm:StorageLvm":
        "an LVM storage definition of the cluster (the match is the `vm` in `Lvm`); no guest",
    "proxmox:index/storageLvmthin:StorageLvmthin":
        "an LVM-thin storage definition of the cluster (the `vm` in `Lvmthin`); no guest",
    "proxmox:index/virtualEnvironmentStorageLvm:VirtualEnvironmentStorageLvm":
        "the older name of storageLvm: an LVM storage definition; no guest",
    "proxmox:index/virtualEnvironmentStorageLvmthin:VirtualEnvironmentStorageLvmthin":
        "the older name of storageLvmthin: an LVM-thin storage definition; no guest",
}

# Provider-local properties of the two guest resources the kit renders: the
# provider keeps them in state and never sends a change of them to the
# hypervisor, so a difference in nothing else is applied without a task on the
# node and without a reboot (`fleetkit adopt`: status `import+local`).
#
# They are the `timeout_*` arguments, from the provider source at v0.115.0:
#   container (proxmoxtf/resource/container/container.go)
#     containerUpdate has no HasChange for any of them, builds the PUT /config
#     body from the other arguments and skips the PUT when the body is empty
#     ("timeouts ... (provider-local) don't trigger an empty-body PUT").
#     timeout_create / timeout_clone / timeout_update / timeout_delete bound
#     the provider's own wait (context.WithTimeout) and are the `timeout` of a
#     shutdown or reboot it makes for another reason; timeout_start
#     (deprecated) is read nowhere, and is the one containerRead does not
#     fill in after an import (seen on a real container, fleetkit#61: applied
#     with no task on the node and no reboot).
#   vm (proxmoxtf/resource/vm/vm.go): from source only, not observed
#     vmUpdate has no HasChange for any of them either and calls UpdateVM only
#     when the body is not empty. They bound the provider's wait for a clone,
#     create, migration, reboot, shutdown, start or stop that something else
#     asked for; timeout_move_disk (deprecated) is read nowhere. vmReadCustom
#     fills all eight in after an import, so a difference needs a declaration.
# cli/tests/test_guests.py holds the list against the pinned name map: every
# `timeout*` property of the two types is here, and nothing else.
LOCAL_PROPERTIES = {
    "proxmox:index/virtualEnvironmentContainer:VirtualEnvironmentContainer": frozenset({
        "timeoutClone", "timeoutCreate", "timeoutDelete", "timeoutStart", "timeoutUpdate",
    }),
    "proxmox:index/virtualEnvironmentVm:VirtualEnvironmentVm": frozenset({
        "timeoutClone", "timeoutCreate", "timeoutMigrate", "timeoutMoveDisk", "timeoutReboot",
        "timeoutShutdownVm", "timeoutStartVm", "timeoutStopVm",
    }),
}

# What a token must match to need a decision (case-insensitive).
GUEST_PATTERN = r"vm|container|lxc"
