"""Which resource types are guests, and which other types of the provider the
guard knows. Data only, no imports: the gate that keeps these lists complete
(tests/guest_types.py) loads the file by path, without the package's
dependencies.

A guest is what a replace or a delete destroys and an in-place update reboots:
every resource type of the bridged bpg/proxmox provider that is a container or
a virtual machine. An HA resource is no guest, but it tells the cluster's HA
manager which state a guest must be in (started, stopped, on which node), so
creating, changing or deleting one can stop or move a guest: the guard gates
it too (guard.py).

Every resource token of the pinned name map
(providers/pulumi/names/bpg-proxmox-*.json) is in exactly one of the sets
below. `tests/guest_types.py` (a gate) and `cli/tests/test_guests.py` fail
when the provider has a token that is in none (a provider bump that adds a
type, guest-like or not, must be decided here), and when a token whose name
matches vm / container / lxc is anywhere but in the guests or in NOT_GUESTS
with its reason. At run time the guard refuses a resource whose type is of a
`proxmox*` package and in none of these sets (guard.unknown_refusals): a type
nobody decided is never applied as if it were harmless.
"""

# Containers and virtual machines, by what they are in Proxmox. A vmid names
# one container or one VM, whichever of the provider's type tokens declares it,
# so the tokens of one family address the same real resource (adopt.py).
CONTAINER_TYPES = frozenset({
    "proxmox:index/virtualEnvironmentContainer:VirtualEnvironmentContainer",
})
VM_TYPES = frozenset({
    "proxmox:index/virtualEnvironmentVm:VirtualEnvironmentVm",
    "proxmox:index/virtualEnvironmentVm2:VirtualEnvironmentVm2",
    "proxmox:index/vm:Vm",
    "proxmox:index/clonedVm:ClonedVm",
    "proxmox:index/virtualEnvironmentClonedVm:VirtualEnvironmentClonedVm",
})
GUEST_TYPES = CONTAINER_TYPES | VM_TYPES

# HA resources: gated like a guest, and their create as well (guard.py).
HA_TYPES = frozenset({
    "proxmox:index/haresource:Haresource",
    "proxmox:index/virtualEnvironmentHaresource:VirtualEnvironmentHaresource",
})

# What the guard gates: a replace, delete or update of these needs naming.
GATED_TYPES = GUEST_TYPES | HA_TYPES

# Tokens the guest pattern matches that are not guests, each with why.
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

# What a token must match to need a decision with a reason (case-insensitive).
GUEST_PATTERN = r"vm|container|lxc"

# Every other resource type of the pinned provider: known, and not gated. A
# type that stops, moves or destroys a guest does not belong here.
OTHER_TYPES = frozenset({
    "proxmox:index/acl:Acl",
    "proxmox:index/acmeAccount:AcmeAccount",
    "proxmox:index/acmeCertificate:AcmeCertificate",
    "proxmox:index/acmeDnsPlugin:AcmeDnsPlugin",
    "proxmox:index/aptRepository:AptRepository",
    "proxmox:index/aptStandardRepository:AptStandardRepository",
    "proxmox:index/backupJob:BackupJob",
    "proxmox:index/cephPool:CephPool",
    "proxmox:index/clusterOptions:ClusterOptions",
    "proxmox:index/downloadFile:DownloadFile",
    "proxmox:index/hagroup:Hagroup",
    "proxmox:index/hardwareMappingDir:HardwareMappingDir",
    "proxmox:index/hardwareMappingPci:HardwareMappingPci",
    "proxmox:index/hardwareMappingUsb:HardwareMappingUsb",
    "proxmox:index/harule:Harule",
    "proxmox:index/metricsServer:MetricsServer",
    "proxmox:index/networkApplier:NetworkApplier",
    "proxmox:index/networkLinuxBond:NetworkLinuxBond",
    "proxmox:index/networkLinuxBridge:NetworkLinuxBridge",
    "proxmox:index/networkLinuxVlan:NetworkLinuxVlan",
    "proxmox:index/nodeConfig:NodeConfig",
    "proxmox:index/nodeDiskZfs:NodeDiskZfs",
    "proxmox:index/nodeFirewall:NodeFirewall",
    "proxmox:index/ociImage:OciImage",
    "proxmox:index/poolMembership:PoolMembership",
    "proxmox:index/realmLdap:RealmLdap",
    "proxmox:index/realmOpenid:RealmOpenid",
    "proxmox:index/realmSync:RealmSync",
    "proxmox:index/replication:Replication",
    "proxmox:index/sdnApplier:SdnApplier",
    "proxmox:index/sdnControllerEvpn:SdnControllerEvpn",
    "proxmox:index/sdnFabricNodeOpenfabric:SdnFabricNodeOpenfabric",
    "proxmox:index/sdnFabricNodeOspf:SdnFabricNodeOspf",
    "proxmox:index/sdnFabricOpenfabric:SdnFabricOpenfabric",
    "proxmox:index/sdnFabricOspf:SdnFabricOspf",
    "proxmox:index/sdnSubnet:SdnSubnet",
    "proxmox:index/sdnVnet:SdnVnet",
    "proxmox:index/sdnZoneEvpn:SdnZoneEvpn",
    "proxmox:index/sdnZoneQinq:SdnZoneQinq",
    "proxmox:index/sdnZoneSimple:SdnZoneSimple",
    "proxmox:index/sdnZoneVlan:SdnZoneVlan",
    "proxmox:index/sdnZoneVxlan:SdnZoneVxlan",
    "proxmox:index/storageCifs:StorageCifs",
    "proxmox:index/storageDirectory:StorageDirectory",
    "proxmox:index/storageNfs:StorageNfs",
    "proxmox:index/storagePbs:StoragePbs",
    "proxmox:index/storageZfspool:StorageZfspool",
    "proxmox:index/userToken:UserToken",
    "proxmox:index/virtualEnvironmentAcl:VirtualEnvironmentAcl",
    "proxmox:index/virtualEnvironmentAcmeAccount:VirtualEnvironmentAcmeAccount",
    "proxmox:index/virtualEnvironmentAcmeCertificate:VirtualEnvironmentAcmeCertificate",
    "proxmox:index/virtualEnvironmentAcmeDnsPlugin:VirtualEnvironmentAcmeDnsPlugin",
    "proxmox:index/virtualEnvironmentAptRepository:VirtualEnvironmentAptRepository",
    "proxmox:index/virtualEnvironmentAptStandardRepository:VirtualEnvironmentAptStandardRepository",
    "proxmox:index/virtualEnvironmentCertificate:VirtualEnvironmentCertificate",
    "proxmox:index/virtualEnvironmentClusterFirewall:VirtualEnvironmentClusterFirewall",
    "proxmox:index/virtualEnvironmentClusterFirewallSecurityGroup:VirtualEnvironmentClusterFirewallSecurityGroup",
    "proxmox:index/virtualEnvironmentClusterOptions:VirtualEnvironmentClusterOptions",
    "proxmox:index/virtualEnvironmentDns:VirtualEnvironmentDns",
    "proxmox:index/virtualEnvironmentDownloadFile:VirtualEnvironmentDownloadFile",
    "proxmox:index/virtualEnvironmentFile:VirtualEnvironmentFile",
    "proxmox:index/virtualEnvironmentFirewallAlias:VirtualEnvironmentFirewallAlias",
    "proxmox:index/virtualEnvironmentFirewallIpset:VirtualEnvironmentFirewallIpset",
    "proxmox:index/virtualEnvironmentFirewallOptions:VirtualEnvironmentFirewallOptions",
    "proxmox:index/virtualEnvironmentFirewallRules:VirtualEnvironmentFirewallRules",
    "proxmox:index/virtualEnvironmentGroup:VirtualEnvironmentGroup",
    "proxmox:index/virtualEnvironmentHagroup:VirtualEnvironmentHagroup",
    "proxmox:index/virtualEnvironmentHardwareMappingDir:VirtualEnvironmentHardwareMappingDir",
    "proxmox:index/virtualEnvironmentHardwareMappingPci:VirtualEnvironmentHardwareMappingPci",
    "proxmox:index/virtualEnvironmentHardwareMappingUsb:VirtualEnvironmentHardwareMappingUsb",
    "proxmox:index/virtualEnvironmentHarule:VirtualEnvironmentHarule",
    "proxmox:index/virtualEnvironmentHosts:VirtualEnvironmentHosts",
    "proxmox:index/virtualEnvironmentMetricsServer:VirtualEnvironmentMetricsServer",
    "proxmox:index/virtualEnvironmentNetworkLinuxBridge:VirtualEnvironmentNetworkLinuxBridge",
    "proxmox:index/virtualEnvironmentNetworkLinuxVlan:VirtualEnvironmentNetworkLinuxVlan",
    "proxmox:index/virtualEnvironmentNodeFirewall:VirtualEnvironmentNodeFirewall",
    "proxmox:index/virtualEnvironmentOciImage:VirtualEnvironmentOciImage",
    "proxmox:index/virtualEnvironmentPool:VirtualEnvironmentPool",
    "proxmox:index/virtualEnvironmentPoolMembership:VirtualEnvironmentPoolMembership",
    "proxmox:index/virtualEnvironmentRealmLdap:VirtualEnvironmentRealmLdap",
    "proxmox:index/virtualEnvironmentRealmOpenid:VirtualEnvironmentRealmOpenid",
    "proxmox:index/virtualEnvironmentRealmSync:VirtualEnvironmentRealmSync",
    "proxmox:index/virtualEnvironmentReplication:VirtualEnvironmentReplication",
    "proxmox:index/virtualEnvironmentRole:VirtualEnvironmentRole",
    "proxmox:index/virtualEnvironmentSdnApplier:VirtualEnvironmentSdnApplier",
    "proxmox:index/virtualEnvironmentSdnFabricNodeOpenfabric:VirtualEnvironmentSdnFabricNodeOpenfabric",
    "proxmox:index/virtualEnvironmentSdnFabricNodeOspf:VirtualEnvironmentSdnFabricNodeOspf",
    "proxmox:index/virtualEnvironmentSdnFabricOpenfabric:VirtualEnvironmentSdnFabricOpenfabric",
    "proxmox:index/virtualEnvironmentSdnFabricOspf:VirtualEnvironmentSdnFabricOspf",
    "proxmox:index/virtualEnvironmentSdnSubnet:VirtualEnvironmentSdnSubnet",
    "proxmox:index/virtualEnvironmentSdnVnet:VirtualEnvironmentSdnVnet",
    "proxmox:index/virtualEnvironmentSdnZoneEvpn:VirtualEnvironmentSdnZoneEvpn",
    "proxmox:index/virtualEnvironmentSdnZoneQinq:VirtualEnvironmentSdnZoneQinq",
    "proxmox:index/virtualEnvironmentSdnZoneSimple:VirtualEnvironmentSdnZoneSimple",
    "proxmox:index/virtualEnvironmentSdnZoneVlan:VirtualEnvironmentSdnZoneVlan",
    "proxmox:index/virtualEnvironmentSdnZoneVxlan:VirtualEnvironmentSdnZoneVxlan",
    "proxmox:index/virtualEnvironmentStorageCifs:VirtualEnvironmentStorageCifs",
    "proxmox:index/virtualEnvironmentStorageDirectory:VirtualEnvironmentStorageDirectory",
    "proxmox:index/virtualEnvironmentStorageNfs:VirtualEnvironmentStorageNfs",
    "proxmox:index/virtualEnvironmentStoragePbs:VirtualEnvironmentStoragePbs",
    "proxmox:index/virtualEnvironmentStorageZfspool:VirtualEnvironmentStorageZfspool",
    "proxmox:index/virtualEnvironmentTime:VirtualEnvironmentTime",
    "proxmox:index/virtualEnvironmentUser:VirtualEnvironmentUser",
    "proxmox:index/virtualEnvironmentUserToken:VirtualEnvironmentUserToken",
})

# Every type the guard knows of a `proxmox*` package.
KNOWN_TYPES = GATED_TYPES | frozenset(NOT_GUESTS) | OTHER_TYPES
