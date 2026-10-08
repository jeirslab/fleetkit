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

# What a token must match to need a decision (case-insensitive).
GUEST_PATTERN = r"vm|container|lxc"
