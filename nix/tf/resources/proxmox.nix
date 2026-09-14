{ config, lib, pkgs, stackId ? null, fleetLib ? null, ... }:

# Emits proxmox non-OS resources (bridges, pools, ACLs, realms, DNS,
# downloads, files) that belong to the current stack (stackId).
#
# Module called once per leaf stack — only entries whose
# "${env}.${stack}" matches the active stackId get emitted.
#
# pkgs flows through to mkFile so it can resolve nix-built sources like
# the prepared NixOS LXC bootstrap template (fleetLib.images, ADR-0003).

let
  helpers = import ../../lib/tf/proxmox.nix { inherit config lib pkgs fleetLib; };

  resourcesInStack = lib.filterAttrs
    (_: r: "${r.env}.${r.stack}" == stackId)
    config.fleet.resources;

  byKind = lib.groupBy (r: r.kind)
    (lib.mapAttrsToList
      (name: meta: meta // { _name = name; })
      resourcesInStack);

  # Per-kind emit: { "<name>" = <tf-block>; ... }
  emitKind = entries: handler:
    lib.listToAttrs (map (e: { name = e._name; value = handler e._name e; }) entries);

  bridgeEntries          = byKind.bridge or [];
  poolEntries            = byKind.pool or [];
  groupEntries           = byKind.group or [];
  aclEntries             = byKind.acl or [];
  dnsEntries             = byKind.dns or [];
  downloadEntries        = byKind.download or [];
  fileEntries            = byKind.file or [];
  clusterOptionsEntries  = byKind."cluster-options" or [];
  metricsServerEntries   = byKind."metrics-server" or [];
  sdnZoneEntries         = byKind."sdn-zone" or [];
  sdnVnetEntries         = byKind."sdn-vnet" or [];
  sdnSubnetEntries       = byKind."sdn-subnet" or [];
  linuxVlanEntries       = byKind."linux-vlan" or [];
  storageNfsEntries      = byKind."storage-nfs" or [];
  storageDirEntries      = byKind."storage-dir" or [];

  sdnEntries = sdnZoneEntries ++ sdnVnetEntries ++ sdnSubnetEntries;
  instanceOf = e: builtins.elemAt (lib.strings.splitString "." e.provider_instance) 1;
  sdnAddr = e:
    if e.kind == "sdn-zone" then "proxmox_sdn_zone_${e.zone_type or "simple"}.${e._name}"
    else if e.kind == "sdn-vnet" then "proxmox_sdn_vnet.${e._name}"
    else "proxmox_sdn_subnet.${e._name}";
  # One applier per provider instance that has SDN entries in this stack.
  sdnAppliers = lib.mapAttrs (inst: es: helpers.mkSdnApplier inst (map sdnAddr es))
    (lib.groupBy instanceOf sdnEntries);
  zonesByType = lib.groupBy (e: e.zone_type or "simple") sdnZoneEntries;

in {
  config = lib.mkIf (stackId != null && resourcesInStack != {}) (lib.mkMerge [
    (lib.mkIf (bridgeEntries != []) {
      resource.proxmox_network_linux_bridge = emitKind bridgeEntries helpers.mkBridge;
    })
    (lib.mkIf (poolEntries != []) {
      resource.proxmox_virtual_environment_pool = emitKind poolEntries helpers.mkPool;
    })
    (lib.mkIf (groupEntries != []) {
      resource.proxmox_virtual_environment_group = emitKind groupEntries helpers.mkGroup;
    })
    (lib.mkIf (aclEntries != []) {
      resource.proxmox_acl = emitKind aclEntries helpers.mkAcl;
    })
    (lib.mkIf (dnsEntries != []) {
      resource.proxmox_virtual_environment_dns = emitKind dnsEntries helpers.mkDns;
    })
    (lib.mkIf (downloadEntries != []) {
      resource.proxmox_download_file = emitKind downloadEntries helpers.mkDownload;
    })
    (lib.mkIf (fileEntries != []) {
      resource.proxmox_virtual_environment_file = emitKind fileEntries helpers.mkFile;
    })
    (lib.mkIf (clusterOptionsEntries != []) {
      # bpg/proxmox renamed proxmox_virtual_environment_cluster_options →
      # proxmox_cluster_options; the old name is removed in v1.0. Existing
      # state was migrated via `tofu state mv` (see commit message).
      resource.proxmox_cluster_options = emitKind clusterOptionsEntries helpers.mkClusterOptions;
    })
    (lib.mkIf (metricsServerEntries != []) {
      # bpg renamed proxmox_virtual_environment_metrics_server →
      # proxmox_metrics_server (old name removed in v1.0), same as
      # cluster-options above. Use the new name from the start.
      resource.proxmox_metrics_server =
        emitKind metricsServerEntries helpers.mkMetricsServer;
    })
    (lib.mkIf (zonesByType ? simple) {
      resource.proxmox_sdn_zone_simple = emitKind zonesByType.simple helpers.mkSdnZone;
    })
    (lib.mkIf (zonesByType ? vlan) {
      resource.proxmox_sdn_zone_vlan = emitKind zonesByType.vlan helpers.mkSdnZone;
    })
    (lib.mkIf (sdnVnetEntries != []) {
      resource.proxmox_sdn_vnet = emitKind sdnVnetEntries helpers.mkSdnVnet;
    })
    (lib.mkIf (sdnSubnetEntries != []) {
      resource.proxmox_sdn_subnet = emitKind sdnSubnetEntries helpers.mkSdnSubnet;
    })
    (lib.mkIf (sdnEntries != []) {
      resource.proxmox_sdn_applier = lib.mapAttrs' (inst: v: lib.nameValuePair "${inst}-sdn" v) sdnAppliers;
    })
    (lib.mkIf (linuxVlanEntries != []) {
      resource.proxmox_virtual_environment_network_linux_vlan = emitKind linuxVlanEntries helpers.mkLinuxVlan;
    })
    (lib.mkIf (storageNfsEntries != []) {
      resource.proxmox_storage_nfs = emitKind storageNfsEntries helpers.mkStorageNfs;
    })
    (lib.mkIf (storageDirEntries != []) {
      resource.proxmox_storage_directory = emitKind storageDirEntries helpers.mkStorageDir;
    })
  ]);
}
