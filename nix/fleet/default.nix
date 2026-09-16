{ config, lib, ... }:

# Unified Proxmox/XCP-ng fleet manifest — schema v2.
#
# Single source of truth for:
#   1. tf emitters in nix/tf/        (TF JSON per-leaf-stack)
#   2. NixOS/Colmena per-host config (via mkHosts in nix/lib/nixos.nix)
#
# Philosophy: env / stack / provider_instance / kind are PER-RESOURCE
# attributes. No structural env→provider binding. One tfstate per
# leaf (env, stack) pair. See docs/plans/unified-swinging-orbit.md.

let
  inherit (lib) attrValues attrNames filter length mapAttrs mapAttrsToList
                elem intersectLists groupBy filterAttrs throwIf
                concatStringsSep hasAttrByPath optionalAttrs;

  # Validator runs below; all attributes from providers/fleet/resources
  # must already be merged when this evaluates.
  cfg = config.fleet;

  # `enabled = false` entries are filtered out before validation and
  # emission so they behave like commented-out declarations. The flag
  # is currently compute-only (resources have no lifecycle to gate);
  # extend to resources if a similar "declared but not realised" need
  # ever appears there.
  enabledCompute = filterAttrs (_: e: e.enabled or true) cfg.compute;

  # `provisioning = "external"` entries (INFRA-170 / ADR-080) are
  # NixOS-managed only: they stay in hostsJson (Colmena target, fleet
  # remote/inventory) but never reach the Terraform emitters or the
  # provider-coupled validators — their machine lives outside this
  # repo's providers.
  provisionedCompute = filterAttrs (_: e: (e.provisioning or "managed") == "managed") enabledCompute;

  # Union of tofu-provisioned compute + resources — for the stack
  # emitters and every validator that assumes a fleet.providers entry.
  allEntries = (attrValues provisionedCompute) ++ (attrValues cfg.resources);
  entriesByName = enabledCompute // cfg.resources;

  # ── Derived: fleet.stacks ───────────────────────────────────────
  # Flat map { "[<fleet>.]${env}.${stack}" = [ entry ... ]; } — consumed
  # by nix/tf/default.nix to emit one terranixConfiguration per leaf.
  # Entries from a named fleet namespace (ADR-097) enumerate with their
  # fleet_ns as stack-ID prefix, which flows into the slug and thus the
  # tofu state key (tf/<fleet>-<env>-<stack>/) — per-fleet separation
  # inside the shared bucket, with the incumbent's keys untouched. The
  # CLI's dot-path prefix matching gives per-fleet stack selection with
  # no new flags (`fleet deploy tf preview <fleet>`).
  stackIdOf = e:
    (lib.optionalString ((e.fleet_ns or null) != null) "${e.fleet_ns}.")
    + "${e.env}.${e.stack}";
  stacksById = groupBy stackIdOf allEntries;

  # ── Validator helpers ──────────────────────────────────────────
  # 1. Unique vm_id within (provider_instance, kind) — NOT global.
  computeGroups = groupBy (e: "${e.provider_instance}/${e.kind}")
    (filter (e: e ? vm_id) (attrValues enabledCompute));
  duplicateVmIds = lib.concatMap (group:
    let
      byVmid = groupBy (e: toString e.vm_id) group;
      dupes = filterAttrs (_: xs: length xs > 1) byVmid;
    in
      mapAttrsToList (vmid: xs:
        "${builtins.head xs}.provider_instance=${(builtins.head xs).provider_instance} vm_id=${vmid} used by ${toString (length xs)} resources"
      ) dupes
  ) (attrValues (groupBy (e: "${e.provider_instance}/${e.kind}") (filter (e: e ? vm_id) (attrValues cfg.compute))));

  # Rebuild clearly: for each (pi, kind) group, find duplicate vm_ids
  # (across enabled entries only — disabled entries are filtered above).
  computeKeyed = lib.mapAttrs (_: group:
    filterAttrs (_: xs: length xs > 1) (groupBy (e: toString e.vm_id) group)
  ) computeGroups;
  vmidViolations = lib.flatten (mapAttrsToList (groupKey: dupes:
    mapAttrsToList (vmid: entries:
      "vm_id=${vmid} in ${groupKey} used by: ${concatStringsSep ", " (map (e: builtins.head (attrNames (filterAttrs (_: v: v == e) entriesByName))) entries)}"
    ) dupes
  ) computeKeyed);

  # 2. Stateful-tag → protect OR provider_instance has destruction_policy=strict.
  providerPolicy = pi:
    let
      parts = lib.strings.splitString "." pi;
      provider = builtins.elemAt parts 0;
      instance = builtins.elemAt parts 1;
    in
      cfg.providers.${provider}.${instance}.destruction_policy or "standard";

  statefulViolations = filter
    (e:
      let
        tags = e.tags or [];
        hits = intersectLists tags cfg.STATEFUL_TAGS;
        policy = providerPolicy e.provider_instance;
      in
        hits != [] && !(e.protect or false) && policy != "strict"
    )
    allEntries;

  # 3. provider_instance must exist in fleet.providers.
  providerRefViolations = filter
    (e:
      let
        parts = lib.strings.splitString "." e.provider_instance;
        provider = builtins.elemAt parts 0;
        instance = if length parts >= 2 then builtins.elemAt parts 1 else "";
      in
        !(hasAttrByPath [ provider instance ] cfg.providers)
    )
    allEntries;

  # 4. compute.pool must match a declared fleet.resources pool entry's
  # pool_id (scoped to the same provider_instance — pools are per-PVE).
  declaredPools = lib.unique (mapAttrsToList
    (_: r: "${r.provider_instance}/${r.pool_id}")
    (filterAttrs (_: r: r.kind == "pool") cfg.resources));
  poolRefViolations = filter
    (e: e.pool != null
        && !(elem "${e.provider_instance}/${e.pool}" declaredPools))
    (attrValues provisionedCompute);

  # 5. Multi-node PVE clusters require explicit `node` on every
  #    resource that lands on a specific member. bpg/proxmox takes
  #    node_name on every container/VM/bridge/file/etc. — without it
  #    the resource would silently land on the provider's primary,
  #    which is rarely what you want when there are 5 members.
  #
  #    Cluster-wide kinds (pool, acl, realm, cluster-options) skip
  #    this — bpg doesn't take node_name for them.
  nodeScopedResourceKinds = [ "bridge" "file" "download" "dns" "linux-vlan" ];

  isProxmoxMultiNode = pi:
    let
      parts = lib.strings.splitString "." pi;
      provider = builtins.elemAt parts 0;
      instance = if length parts >= 2 then builtins.elemAt parts 1 else "";
      providerCfg = (cfg.providers.${provider} or {}).${instance} or null;
    in
      provider == "proxmox"
      && providerCfg != null
      && length (providerCfg.cluster.nodes or []) > 1;

  computeNodeViolations = filter
    (e: isProxmoxMultiNode e.provider_instance && (e.node or "") == "")
    (attrValues provisionedCompute);

  resourceNodeViolations = filter
    (e: elem e.kind nodeScopedResourceKinds
        && isProxmoxMultiNode e.provider_instance
        && (e.node or "") == "")
    (attrValues cfg.resources);

  # 6. Prefix lengths, when set explicitly, must agree with the CIDRs
  #    they default from (a stale override silently mis-addresses every
  #    legacy-mode host).
  prefixOf = cidr: lib.toInt (lib.last (lib.splitString "/" cidr));
  prefixViolations =
    lib.optional (cfg.network.internal_cidr != null
                  && prefixOf cfg.network.internal_cidr != cfg.network.internal_prefix_len)
      "fleet.network.internal_prefix_len=${toString cfg.network.internal_prefix_len} disagrees with internal_cidr=${cfg.network.internal_cidr}"
    ++ lib.optional (cfg.network.lan_cidr != null
                     && prefixOf cfg.network.lan_cidr != cfg.network.lan_prefix_len)
      "fleet.network.lan_prefix_len=${toString cfg.network.lan_prefix_len} disagrees with lan_cidr=${cfg.network.lan_cidr}";

  # 7. LXC-only knobs on a VM are a mistake, not a no-op.
  nameOf = e: builtins.head (attrNames (filterAttrs (_: v: v == e) entriesByName));
  lxcOnlyOnVm = filter
    (e: e.kind == "vm"
        && ((e.devices or []) != [] || (e.hook_script or null) != null || (e.lxc_extra_conf or []) != []
            || (e.arch or "amd64") != "amd64"
            || (e.features.mknod or false) || (e.features.mount or []) != []))
    (attrValues enabledCompute);

  # 7b. lxc_extra_conf needs a node to SSH to (explicit or the instance's
  #     primary_node).
  extraConfViolations = lib.concatMap (e:
    let
      parts = lib.strings.splitString "." e.provider_instance;
      inst = if length parts >= 2 then builtins.elemAt parts 1 else "";
      primary = ((cfg.providers.proxmox or {}).${inst} or { cluster.primary_node = ""; }).cluster.primary_node;
    in lib.optional ((e.lxc_extra_conf or []) != [] && (e.node or "") == "" && primary == "")
         "${nameOf e}: lxc_extra_conf needs `node` (or cluster.primary_node on ${e.provider_instance}) to reach the PVE node over SSH")
    (attrValues provisionedCompute);

  # 8. Device passthrough: unique host paths per guest; DNS override
  #    non-empty when given.
  deviceViolations = lib.concatMap
    (e: let paths = map (d: d.path) (e.devices or []);
        in lib.optional (lib.unique paths != paths) "${nameOf e}: duplicate device paths")
    (attrValues enabledCompute);
  dnsViolations = lib.concatMap
    (e: lib.optional ((e.dns.servers or null) == []) "${nameOf e}: dns.servers = [] (use null to inherit fleet.network.dns_servers)")
    (attrValues enabledCompute);

  # 9. Declared network mode: interfaces present iff declared; per-NIC
  #    consistency; internal_ip / ip must be one of the static
  #    addresses (hostsJson, colmena targetHost and DNS use them).
  hostPart = cidr: builtins.head (lib.splitString "/" cidr);
  isCidr = v: v != null && v != "dhcp" && v != "manual";
  nicViolations = lib.concatMap (e:
    let
      n = nameOf e;
      ifs = e.interfaces or [];
      declared = (e.network_mode or "") == "declared";
      names = lib.imap0 (i: x: if x.name != null then x.name else "eth${toString i}") ifs;
      statics = map hostPart (map (x: x.ipv4) (filter (x: isCidr x.ipv4) ifs));
      v4gw = length (filter (x: x.gateway != null) ifs);
      v6gw = length (filter (x: x.ipv6.gateway != null) ifs);
    in
      lib.optional (declared && ifs == []) "${n}: network_mode = \"declared\" needs at least one entry in `interfaces`"
      ++ lib.optional (!declared && ifs != []) "${n}: `interfaces` is only read by network_mode = \"declared\" (current: ${e.network_mode})"
      ++ lib.optional (lib.unique names != names) "${n}: duplicate interface names"
      ++ lib.optional (e.kind == "container" && lib.any (x: builtins.match "eth[0-9]+" x == null) names) "${n}: LXC interface names must be ethN (the NixOS side assumes it)"
      ++ lib.optional (v4gw > 1) "${n}: more than one interface carries an IPv4 gateway"
      ++ lib.optional (v6gw > 1) "${n}: more than one interface carries an IPv6 gateway"
      ++ lib.concatMap (x:
           lib.optional (x.vnet != null && x.bridge != "vmbr0") "${n}: set either `bridge` or `vnet` on an interface, not both"
           ++ lib.optional (x.gateway != null && !(isCidr x.ipv4)) "${n}: an IPv4 gateway needs a static ipv4 CIDR"
           ++ lib.optional (x.ipv6.method == "static" && x.ipv6.address == null) "${n}: ipv6.method = static needs ipv6.address"
           ++ lib.optional (x.ipv6.method != "static" && x.ipv6.address != null) "${n}: ipv6.address is only used with ipv6.method = static"
           ++ lib.optional (x.ipv6.method != "static" && x.ipv6.gateway != null) "${n}: ipv6.gateway is only used with ipv6.method = static")
         ifs
      ++ lib.optional (declared && (e.internal_ip or "") != "" && !(elem e.internal_ip statics)) "${n}: internal_ip ${e.internal_ip} is not the address of any static interface"
      ++ lib.optional (declared && (e.ip or "") != "" && !(elem e.ip statics)) "${n}: ip ${e.ip} is not the address of any static interface"
  ) (attrValues enabledCompute);

  # 11. SDN references: an interface's `vnet` and a subnet's `vnet` must
  #     name an sdn-vnet resource on the same provider instance; a vnet's
  #     `zone` an sdn-zone there; a vlan zone needs `bridge`.
  declaredVnets = lib.unique (mapAttrsToList (n: r: "${r.provider_instance}/${r.vnet_id or n}")
    (filterAttrs (_: r: r.kind == "sdn-vnet") cfg.resources));
  declaredZones = lib.unique (mapAttrsToList (n: r: "${r.provider_instance}/${r.zone_id or n}")
    (filterAttrs (_: r: r.kind == "sdn-zone") cfg.resources));
  sdnViolations =
    lib.concatMap (e: lib.concatMap (x:
        lib.optional (x.vnet != null && !(elem "${e.provider_instance}/${x.vnet}" declaredVnets))
          "${nameOf e}: interface vnet \"${x.vnet}\" has no sdn-vnet resource on ${e.provider_instance}")
      (e.interfaces or [])) (attrValues provisionedCompute)
    ++ mapAttrsToList (n: r: "${n}: sdn-vnet zone \"${r.zone}\" has no sdn-zone resource on ${r.provider_instance}")
      (filterAttrs (n: r: r.kind == "sdn-vnet" && !(elem "${r.provider_instance}/${r.zone}" declaredZones)) cfg.resources)
    ++ mapAttrsToList (n: r: "${n}: sdn-subnet vnet \"${r.vnet}\" has no sdn-vnet resource on ${r.provider_instance}")
      (filterAttrs (n: r: r.kind == "sdn-subnet" && !(elem "${r.provider_instance}/${r.vnet}" declaredVnets)) cfg.resources)
    ++ mapAttrsToList (n: r: "${n}: sdn-zone of zone_type vlan needs `bridge`")
      (filterAttrs (n: r: r.kind == "sdn-zone" && (r.zone_type or "simple") == "vlan" && !(r ? bridge)) cfg.resources);

  # 10. VM-only hardware settings on a container are a mistake.
  vmOnlyOnLxc = filter
    (e: e.kind == "container" && (
      let v = e.vm; in
      v.machine != null || v.bios != null || v.scsi_hardware != null || v.boot_order != null
      || v.tablet != null || v.agent != null || v.cpu_type != "host" || !v.serial_console
      || v.root_disk.interface != "virtio0" || v.root_disk.cache != null || v.root_disk.discard != null
      || v.root_disk.iothread != null || v.root_disk.ssd != null
      || !e.cloud_init.enable || e.cloud_init.datastore != null))
    (attrValues enabledCompute);

  # ── Throw chain ────────────────────────────────────────────────
  validated =
    throwIf (sdnViolations != [])
      ''
        FLEET SDN reference violations:
          ${concatStringsSep "\n  " sdnViolations}
      '' (
    throwIf (vmOnlyOnLxc != [])
      ''
        FLEET VM-only options (vm.* / cloud_init.enable / cloud_init.datastore) set on container entries:
          ${concatStringsSep ", " (map nameOf vmOnlyOnLxc)}
      '' (
    throwIf (nicViolations != [])
      ''
        FLEET interface violations:
          ${concatStringsSep "\n  " nicViolations}
      '' (
    throwIf (lxcOnlyOnVm != [])
      ''
        FLEET LXC-only options set on VM entries (devices / hook_script / arch / features.mknod / features.mount):
          ${concatStringsSep ", " (map nameOf lxcOnlyOnVm)}
      '' (
    throwIf ((deviceViolations ++ dnsViolations ++ extraConfViolations) != [])
      ''
        FLEET compute violations:
          ${concatStringsSep "\n  " (deviceViolations ++ dnsViolations ++ extraConfViolations)}
      '' (
    throwIf (prefixViolations != [])
      ''
        FLEET network prefix-length violations:
          ${concatStringsSep "\n  " prefixViolations}
        Drop the explicit *_prefix_len (it derives from the CIDR) or fix the CIDR.
      '' (
    throwIf (vmidViolations != [])
      ''
        FLEET vm_id uniqueness violations (scope: provider_instance + kind):
          ${concatStringsSep "\n  " vmidViolations}
      '' (
    throwIf (statefulViolations != [])
      ''
        FLEET stateful-tag violations (tagged resource without protect=true
        and provider_instance.destruction_policy != "strict"):
          ${concatStringsSep ", " (map (e:
            let
              name = builtins.head (attrNames (filterAttrs (_: v: v == e) entriesByName));
              hits = intersectLists (e.tags or []) cfg.STATEFUL_TAGS;
            in "${name} (tags: ${toString hits})"
          ) statefulViolations)}
      '' (
    throwIf (providerRefViolations != [])
      ''
        FLEET entries reference undefined provider_instance:
          ${concatStringsSep ", " (map (e: e.provider_instance) providerRefViolations)}
      '' (
    throwIf (poolRefViolations != [])
      ''
        FLEET compute entries reference undeclared pool:
          ${concatStringsSep ", " (map (e:
            let
              name = builtins.head (attrNames (filterAttrs (_: v: v == e) entriesByName));
            in "${name} (provider_instance=${e.provider_instance}, pool=${e.pool})"
          ) poolRefViolations)}
        Declare a `kind = "pool"` entry in fleet.resources with matching
        pool_id and provider_instance.
      '' (
    throwIf ((computeNodeViolations ++ resourceNodeViolations) != [])
      ''
        FLEET multi-node cluster targeting violations:
          ${concatStringsSep "\n  " (map (e:
            let
              name = builtins.head (attrNames (filterAttrs (_: v: v == e) entriesByName));
            in "${name} (provider_instance=${e.provider_instance}, kind=${e.kind})"
          ) (computeNodeViolations ++ resourceNodeViolations))}
        Set `node = "<member>"` on each. Available members for
        each provider:
          ${concatStringsSep "\n  " (mapAttrsToList (p: instances:
            concatStringsSep "\n    " (mapAttrsToList (i: c:
              "${p}.${i}: [ ${concatStringsSep " " (c.cluster.nodes or [])} ]"
            ) instances)
          ) cfg.providers)}
      ''
    true))))))))));

in {
  imports = [
    # Per-concept folders — each default.nix declares schema (+ derivation
    # if any) and `imports = [ ./inputs.nix ]` pulls in the values sibling.
    ./network
    ./providers
    ./users                  # fleet.access schema (identity registry is consumer data)
    ./dns                    # fleet.dnsRecords derivation
    ./settings.nix           # fleetkit parameter surface (consumer-supplied values)

    # Flat schemas (values come from nix/hosts/**/, no separable inputs file)
    ./compute.nix
    ./resources.nix
    ./hosts-registry.nix
    ./v2-normalize.nix       # ADR-096: provider-tree authoring → flat normal form
  ];

  options.fleet = {
    STATEFUL_TAGS = lib.mkOption {
      type = lib.types.listOf lib.types.str;
      default = [
        "bitcoin" "indexer" "postgres" "timescaledb"
        "messaging" "auth" "backup" "storage"
      ];
      description = "Tags whose presence forces protect=true (enforced by validator).";
    };

    # Derived exports — read by terranix + mkHosts.
    stacks = lib.mkOption {
      type = lib.types.attrsOf (lib.types.listOf lib.types.attrs);
      default = {};
      internal = true;
      description = ''
        Derived attrset keyed by "ENV.STACK". Consumed by
        nix/tf/default.nix to emit one terranixConfiguration
        per leaf.
      '';
    };

    # Legacy hosts.json-shaped export — keeps mkHosts / fleet launcher /
    # grafana-stack / sssd working during the transition. Built from
    # fleet.compute so there's only one source of truth.
    hostsJson = lib.mkOption {
      type = lib.types.attrsOf lib.types.attrs;
      default = {};
      internal = true;
      description = ''
        { name → { hostname, vmid, ip, internal_ip, tags, pve_type,
                   cluster?, node_index? } } — serialised to
        .cache/fleet/hosts.json by `fleet inventory generate`, consumed by the
        NixOS side (hosts.nix) and by Colmena's deploy targeting.
      '';
    };

    # Derived per-host SSH client data (from hostsJson): the fleet's ssh config
    # as structured data, for consumers to render into ~/.ssh/config.
    sshHosts = lib.mkOption {
      type = lib.types.attrsOf (lib.types.submodule {
        options = {
          hostName = lib.mkOption {
            type = lib.types.str;
            description = "Address ssh connects to — the host's internal fleet IP, else its LAN ip.";
          };
          user = lib.mkOption {
            type = lib.types.str;
            default = "root";
            description = "Login user (fleet hosts are Colmena-deployed as root).";
          };
        };
      });
      default = {};
      internal = true;
      description = ''
        { name → { hostName, user } } derived from hostsJson — the fleet's SSH
        client config as data. Consumers render it into an OpenSSH config (e.g.
        home-manager programs.ssh.matchBlocks) so `ssh <fleet-host>` resolves
        fleet-wide with no hand-maintained ~/.ssh/config, or serialise to JSON.
      '';
    };

    _meta.validated = lib.mkOption {
      type = lib.types.bool;
      default = validated;
      internal = true;
      description = "Sentinel forcing validator evaluation during nix build.";
    };
  };

  config.fleet.stacks = stacksById;

  # hostsJson reflects only enabled entries — disabled installers
  # shouldn't pollute the inventory, get Colmena targets, or appear
  # in `fleet inventory`.
  config.fleet.hostsJson = mapAttrs (name: meta: {
    hostname = name;
    vmid = meta.vm_id;
    ip = meta.ip;
    internal_ip = meta.internal_ip;
    # Ordered fallback deploy addresses (colmena targetHost candidates
    # beyond `ip`) — the CLI probes these for first-reachable. INFRA:
    # externally-reachable paths like a Tailscale IP live here.
    deploy_ips = meta.deploy_ips or [ ];
    tags = meta.tags or [];
    # Despite the field name (kept for hosts.json compatibility),
    # this is the substrate-aware platform tag consumed by the
    # `infra.platform.type` option declared in
    # nix/modules/infra/base/platform/default.nix. Four values today:
    #   "pve.lxc"   — PVE-hosted LXC container (kind = "container")
    #   "pve.qemu"  — PVE-hosted KVM/QEMU VM (kind = "vm" on proxmox.*)
    #   "xcpng.vm"  — XCP-ng-hosted Xen HVM VM (kind = "vm" on
    #                 xen-orchestra.*)
    #   "baremetal" — physical / externally-provisioned host (kind =
    #                 "baremetal"); brings its own bootloader + net stack
    # The substrate split matters because PVE-VM and XCP-ng-VM need
    # very different boot loaders, kernel modules, and console
    # configs — see nix/modules/infra/base/platform/{pve,xcpng}/*.nix,
    # and baremetal cancels the hypervisor-guest assumptions entirely
    # (nix/modules/infra/base/platform/baremetal.nix).
    pve_type =
      if meta.kind == "baremetal" then "baremetal"
      else if meta.kind == "container" then "pve.lxc"
      else if lib.hasPrefix "xen-orchestra." meta.provider_instance then "xcpng.vm"
      else "pve.qemu";
    # Needed by `fleet inventory generate` to know which entries should
    # be IP-discovered via XOA vs. PVE. The Proxmox path also benefits
    # from this for future multi-provider drift detection.
    provider_instance = meta.provider_instance;
    # Non-null for non-NixOS guests (Debian LXCs/VMs booted from an
    # explicit image). `fleet ansible inventory` uses it to place such
    # containers into the debian_guests group; NixOS guests (image ==
    # null) land in the nixos group instead.
    image = meta.image or null;
    # "managed" | "external" — external hosts (INFRA-170 / ADR-080) are
    # Colmena-deployable but must be skipped by consumers that assume
    # fleet-network reachability (CoreDNS records, hypervisor drift).
    provisioning = meta.provisioning or "managed";
    pool = meta.pool or null;
    # Declared NIC layout, for the CLI inventory and a future
    # infra.networking driven by declared interfaces (PLAN.md Appendix A.2).
    network_mode = meta.network_mode or "single-internal";
    interfaces = lib.imap0 (i: n: {
      name = if n.name != null then n.name else "eth${toString i}";
      bridge = if n.vnet != null then n.vnet else n.bridge;
      inherit (n) ipv4 gateway vlan mtu mac;
      ipv6 = n.ipv6.method;
    }) (meta.interfaces or []);
    ssh_groups = meta.ssh_groups or [ "platform-admins" ];
    sudo_groups = meta.sudo_groups or [];
  }) enabledCompute;

  # SSH client view of the fleet, derived from hostsJson: every host with a
  # reachable address, mapped to { hostName, user }. Rendered by consumers into
  # ~/.ssh/config (home-manager programs.ssh) or serialised to JSON.
  config.fleet.sshHosts = mapAttrs (_name: h: {
    hostName = if h.internal_ip != "" then h.internal_ip else h.ip;
    user = "root";
  }) (filterAttrs (_: h: (h.internal_ip or "") != "" || (h.ip or "") != "") config.fleet.hostsJson);
}
