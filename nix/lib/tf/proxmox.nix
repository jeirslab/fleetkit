{ config, lib, pkgs ? null, fleetLib ? null }:

# Emitters that translate a single fleet entry into a bpg/proxmox
# resource block. Consumed by nix/tf/compute/proxmox.nix (for
# LXC + KVM) and nix/tf/resources/proxmox.nix (for everything
# else — bridges, pools, ACLs, realms, DNS, downloads, files,
# cluster-options).

let
  net = config.fleet.network;
  pveSettings = config.fleet.settings.providers.proxmox;
  # Storage for everything a compute entry does not name explicitly.
  ds = pveSettings.defaultDatastore;

  # Gateways are optional (null ⇒ isolated/minimal fleet): omit the
  # gateway attr from ip_config instead of emitting null.
  optGw = gw: lib.optionalAttrs (gw != null) { gateway = gw; };

  # Bare address → CIDR with the fleet-wide prefix length
  # (fleet.network.internal_prefix_len / lan_prefix_len, default 24 —
  # what used to be a literal "/24" here).
  ipv4Cidr = ip: len: "${ip}/${toString len}";
  internalCidr = ip: ipv4Cidr ip net.internal_prefix_len;
  lanCidr = ip: ipv4Cidr ip net.lan_prefix_len;

  # Note renderers (markdown → bpg `description`).
  notes = import ../notes/proxmox { inherit lib; };

  # Resolve which PVE cluster member a given resource lands on.
  # bpg/proxmox takes one provider per cluster and forwards API
  # calls between members; node_name is REQUIRED on every resource
  # to tell the provider where to provision.
  #
  # Precedence:
  #   1. meta.node — explicit per-resource (mandatory on multi-node
  #      clusters; validator in nix/fleet/default.nix enforces).
  #   2. fleet.providers.proxmox.<instance>.cluster.primary_node —
  #      single-node clusters and provider default fall through here.
  resolveNode = meta:
    let
      parts = lib.strings.splitString "." meta.provider_instance;
      inst = builtins.elemAt parts 1;
      providerCfg = config.fleet.providers.proxmox.${inst} or null;
      explicit = meta.node or "";
      fallback = if providerCfg == null then "" else providerCfg.cluster.primary_node;
    in
      if explicit != "" then explicit else fallback;

  # Picks the right description string for an LXC/VM:
  #   - structured `note` (rendered via mkNote) wins,
  #   - else the legacy `notes` string is used as plain markdown,
  #   - else nothing is emitted.
  computeDescription = meta:
    if meta.note or null != null then notes.mkNote meta.note
    else if meta.notes or "" != "" then meta.notes
    else null;

  # Network-mode dispatch — reproduces the Python Declarer logic.
  # Every initialization block carries the cluster-wide DNS list
  # (fleet.network.dns_servers — fleet DNS first, public fallback) so
  # fresh LXCs and VMs boot resolvable even when the fleet's own DNS
  # isn't up yet. mkVm has its own initialization block and adds
  # DNS there directly; LXCs (mkContainer) rely entirely on mkNetwork.
  # Create-time DNS block: per-guest override (meta.dns) falling back to
  # the cluster-wide fleet.network values. dns_domain is optional (null ⇒
  # no internal zone): omit the attr so the provider writes no search
  # domain into the guest; "" on the per-guest override means the same.
  dnsFor = meta:
    let
      d = meta.dns or { servers = null; domain = null; };
      servers = if d.servers != null then d.servers else net.dns_servers;
      domain = if d.domain != null then d.domain else net.dns_domain;
    in { inherit servers; }
       // lib.optionalAttrs (domain != null && domain != "") { inherit domain; };

  # Lifecycle attributes shared by containers and VMs; each emitted only
  # when it differs from what the emitter produced before the option
  # existed (protection off, startup unmanaged).
  lifecycleAttrs = meta:
    let
      # INFRA-227: `startup_order` is a deprecated flat alias of the
      # structured `startup.order`. One knob renders here; the structured
      # form wins when both are set.
      effectiveStartup =
        if (meta.startup or null) != null then meta.startup
        else if (meta.startup_order or null) != null
        then { order = meta.startup_order; up_delay = null; down_delay = null; }
        else null;
    in
    lib.optionalAttrs (meta.protection or false) { protection = true; }
    // lib.optionalAttrs (effectiveStartup != null) {
      startup = lib.filterAttrs (_: v: v != null) {
        inherit (effectiveStartup) order up_delay down_delay;
      };
    };

  # ── Declared-mode NIC helpers ──
  ifName = i: n: if n.name != null then n.name else "eth${toString i}";
  bridgeFor = n: if n.vnet != null then n.vnet else n.bridge;
  nicCommon = n:
    lib.optionalAttrs (n.vlan != null) { vlan_id = n.vlan; }
    // lib.optionalAttrs (n.mtu != null) { mtu = n.mtu; }
    // lib.optionalAttrs (n.mac != null) { mac_address = n.mac; }
    // lib.optionalAttrs (n.rate_limit_mbps != null) { rate_limit = n.rate_limit_mbps; };
  # One ip_config entry per NIC (positional, like PVE's netN). A NIC with
  # no IPv4 and no IPv6 yields {} — verify on a dev node that the pinned
  # provider accepts an empty block; otherwise use ipv4 = "manual".
  ipConfigFor = n:
    lib.optionalAttrs (n.ipv4 != null) { ipv4 = { address = n.ipv4; } // optGw n.gateway; }
    // lib.optionalAttrs (n.ipv6.method != "none") {
      ipv6 = { address = if n.ipv6.method == "static" then n.ipv6.address else n.ipv6.method; }
        // optGw n.ipv6.gateway;
    };
  declaredLxcNics = meta: lib.imap0 (i: n:
    { name = ifName i n; bridge = bridgeFor n; }
    // nicCommon n
    // lib.optionalAttrs n.firewall { firewall = true; }) meta.interfaces;

  mkNetwork = meta: let
    mode = meta.network_mode;
    dnsConfig = dnsFor meta;
    # Per-host override for bpg-provider's `host_managed` attribute. Splice
    # into every network_interface entry when set. null = leave bpg default.
    hmAttrs = if (meta ? host_managed && meta.host_managed != null)
      then { host_managed = meta.host_managed; }
      else { };
    nic = base: base // hmAttrs;

    # Internal-LAN bridge name depends on which PVE cluster we're
    # emitting for. The common shape puts the internal LAN on vmbr1
    # (the host's vmbr0 carries the WAN-side address). Clusters whose
    # PVE nodes have a single NIC carrying internal traffic directly
    # on vmbr0 (e.g. PVE-on-XCP-ng VMs) are listed in
    # fleet.settings.providers.proxmox.singleBridgeInstances — any
    # LXC on those clusters uses vmbr0 for "internal".
    #
    # Dispatch on the provider_instance name; per-host
    # `internal_bridge` override wins when set.
    internalBridgeDefault =
      let parts = lib.strings.splitString "." meta.provider_instance;
          inst  = builtins.elemAt parts 1;
      in if builtins.elem inst config.fleet.settings.providers.proxmox.singleBridgeInstances
         then "vmbr0" else "vmbr1";
    internalBridge =
      if (meta ? internal_bridge && meta.internal_bridge != null && meta.internal_bridge != "")
      then meta.internal_bridge
      else internalBridgeDefault;
  in
    if mode == "single-internal" then {
      network_interface = [(nic { name = "eth0"; bridge = internalBridge; })];
      initialization = {
        hostname = meta._name;
        dns = dnsConfig;
        ip_config = [{
          ipv4 = { address = internalCidr meta.internal_ip; } // optGw net.gateway;
        }];
      };
    }
    # Mirror of single-internal but on vmbr0 with the LAN gateway
    # (fleet.network.lan_gateway). Used for hosts that should live only
    # on the LAN (no vmbr1 NIC, no internal_ip). The fleet's internal
    # DNS / service-alias mesh won't reach the host directly; public
    # ingress is expected to be a LAN-router port-forward straight to
    # `meta.ip`.
    else if mode == "single-external" then {
      network_interface = [(nic { name = "eth0"; bridge = "vmbr0"; })];
      initialization = {
        hostname = meta._name;
        dns = dnsConfig;
        ip_config = [{
          ipv4 = { address = lanCidr meta.ip; } // optGw net.lan_gateway;
        }];
      };
    }
    else if mode == "dual" then {
      network_interface = [
        (nic { name = "eth0"; bridge = "vmbr0"; })
        (nic { name = "eth1"; bridge = "vmbr1"; })
      ];
      initialization = {
        hostname = meta._name;
        dns = dnsConfig;
        ip_config = [{ ipv4 = { address = internalCidr meta.internal_ip; }; }];
      };
    }
    else if mode == "custom-netgate" then {
      network_interface = [
        (nic { name = "eth0"; bridge = "vmbr0"; mac_address = "BC:24:11:5B:EA:26"; })
        (nic { name = "eth1"; bridge = "vmbr1"; })
      ];
      initialization = {
        hostname = "netgate";
        dns = dnsConfig;
        ip_config = [
          { ipv4 = { address = lanCidr meta.ip; } // optGw net.lan_gateway; }
          # netgate IS the vmbr1 gateway — no upstream here.
          { ipv4 = { address = internalCidr meta.internal_ip; }; }
        ];
      };
    }
    else if mode == "custom-btc-testnet" then {
      network_interface = [(nic { name = "eth0"; bridge = "vmbr1"; })];
      initialization = {
        hostname = meta._name;
        dns = dnsConfig;
        ip_config = [{
          ipv4 = { address = internalCidr meta.internal_ip; } // optGw net.gateway;
          ipv6 = { address = "auto"; };
        }];
      };
    }
    # single-internal plus a SECOND NIC on the same flat-L2 internal
    # bridge, carrying a pinned MAC. The use case is an ingress identity
    # a LAN-router port-forward targets by MAC: the host keeps its normal
    # internal address on eth0 and answers a second, historically
    # separate address on eth1. Declaring the NIC here is the point —
    # a hand-added `pct set` NIC is not in the manifest, so the next
    # apply reconciles it away and the ingress silently dies.
    # In-guest addressing belongs in the host's systemd.network config;
    # only the attachment + MAC are provider state. Requires
    # meta.mac_address_eth1.
    else if mode == "internal-plus-lan-mac" then {
      network_interface = [
        (nic { name = "eth0"; bridge = internalBridge; })
        (nic { name = "eth1"; bridge = internalBridge;
               mac_address = meta.mac_address_eth1; })
      ];
      initialization = {
        hostname = meta._name;
        dns = dnsConfig;
        ip_config = [{
          ipv4 = { address = "${meta.internal_ip}/24"; } // optGw net.gateway;
        }];
      };
    }
    # ADR-021: dual-NIC LXC that IS a LAN gateway. eth0/vmbr0 carries
    # the WAN-side address + default route (fleet.network.lan_gateway);
    # eth1/vmbr1 carries the LAN-side address with no gateway (this host
    # IS the gateway for LAN-only containers). Used by the `router` LXC;
    # only works on privileged containers because NAT needs CAP_NET_ADMIN.
    # If meta.mac_address_eth0 is set, pins the WAN NIC's MAC — needed
    # for ingress so LAN-router port-forwards keep landing across recreates.
    else if mode == "lxc-router" then {
      network_interface = [
        (nic ({ name = "eth0"; bridge = "vmbr0"; }
          // lib.optionalAttrs (meta ? mac_address_eth0 && meta.mac_address_eth0 != null)
               { mac_address = meta.mac_address_eth0; }))
        (nic { name = "eth1"; bridge = "vmbr1"; })
      ];
      initialization = {
        hostname = meta._name;
        dns = dnsConfig;
        ip_config = [
          { ipv4 = { address = lanCidr meta.ip; } // optGw net.lan_gateway; }
          { ipv4 = { address = internalCidr meta.internal_ip; }; }
        ];
      };
    }
    # The general form: every NIC declared explicitly (bridge/VNet, DHCP
    # or CIDR with its own prefix, VLAN, MTU, MAC, IPv6, firewall).
    else if mode == "declared" then {
      network_interface = map nic (declaredLxcNics meta);
      initialization = {
        hostname = meta._name;
        dns = dnsConfig;
        ip_config = map ipConfigFor meta.interfaces;
      };
    }
    else throw "mkNetwork: unsupported network_mode ${mode} for ${meta._name}";

  # Resolve the provider alias — every TF resource gets
  # `provider = "proxmox.<instance>"` via `provider` attribute.
  mkProviderRef = providerInstance: providerInstance;

  # Destruction-policy-aware lifecycle block.
  mkLifecycle = meta: let
    parts = lib.strings.splitString "." meta.provider_instance;
    inst = builtins.elemAt parts 1;
    instCfg = config.fleet.providers.proxmox.${inst} or null;
    policy = if instCfg == null then "standard" else instCfg.destruction_policy;
    effectiveProtect = meta.protect
      || policy == "strict"
      || (meta ? tags
          && lib.intersectLists meta.tags config.fleet.STATEFUL_TAGS != []
          && policy != "permissive");
    hasIgnore = (meta.ignore_changes or []) != [];
  in
    lib.optionalAttrs (effectiveProtect || hasIgnore) {
      lifecycle = [(lib.mkMerge [
        (lib.optionalAttrs effectiveProtect { prevent_destroy = true; })
        (lib.optionalAttrs hasIgnore { ignore_changes = meta.ignore_changes; })
      ])];
    };

  # Cluster-wide NFS storage `nix-store` (nix-builder tier-0 VM exports
  # /data/nfs/store; registered via ansible proxmox/pve nfs-storage.yml,
  # 2026-06-11). Templates upload once and every cluster node sees them
  # — replaces the old scp-to-every-node bootstrap. (The 2026-06-06
  # attempt failed because nix-builder was then a PVE LXC that couldn't
  # run kernel nfsd; it's since moved to a tier-0 XCP-ng VM.)
  # The ADR-0003 "latest" handle of the images-family bootstrap LXC
  # template (fleetLib.images.templates.proxmox-lxc.latest). Kept a literal
  # here on purpose: rendering a container's TF must not force building the
  # ~200 MB image, and this name IS the published contract. It must stay in
  # lock-step with the images-family reference and the factory below.
  nixosLxcTemplate = "${pveSettings.lxcTemplateDatastore}:vztmpl/nixos-bootstrap-lxc-latest.tar.xz";
  # The Debian *VM* path (debian13DiskId / debianCloudImage qcow2) was removed
  # (INFRA-137): the fleet has no KVM Debian guests — every non-NixOS guest is a
  # Debian LXC, which uses a vztmpl rootfs tarball (not a VM qcow2). That
  # template should be served cluster-wide from the `nix-store` NFS SR, not a
  # per-node `local:` upload.

  # NixOS LXC bootstrap template, built from the images component family
  # (fleetLib.images.mkBootstrapImage, target "proxmox-lxc"). The nixos-
  # generators tarball has a version-suffixed name that changes every build
  # and would break tofu state diffing, so a thin runCommand copies it to
  # the stable "latest" handle. The bpg/proxmox provider then SCPs that up
  # to PVE's vztmpl store on apply.
  #
  # NB: unlike the previous per-consumer builder, this image is pinned to
  # fleetkit's own nixpkgs (via nixos-generators' `follows`), not the
  # consuming host's — the deliberate "bootstrap is the pinned-library role"
  # split. The template only has to boot and accept the deploy key over SSH;
  # colmena then pushes the real (consumer-pinned) closure.
  preparedNixosLxcTemplatePath =
    if pkgs != null && fleetLib != null
    then
      let
        image = fleetLib.images.mkBootstrapImage {
          target = "proxmox-lxc";
          deployKey = config.fleet.network.sysadmin_ssh_key;
          inherit (config.fleet.settings.cache) substituters trustedPublicKeys;
        };
      in "${pkgs.runCommand "nixos-bootstrap-lxc" { } ''
        mkdir -p $out
        cp ${image}/tarball/*.tar.xz $out/nixos-bootstrap-lxc-latest.tar.xz
      ''}/nixos-bootstrap-lxc-latest.tar.xz"
    else null;

in rec {
  inherit mkNetwork mkLifecycle mkProviderRef nixosLxcTemplate net;

  # ── LXC container emitter ─────────────────────────────────────
  # When `meta.cloneFrom != null`, the container is provisioned by cloning
  # the named source VMID via the bpg/proxmox provider's `clone` block;
  # `clone` and `operating_system`/`template_file_id` are mutually
  # exclusive in the provider, so the OS template block is dropped on the
  # clone path. Mount points are explicitly re-declared either way so the
  # provider tracks them in state (the source's mounts are inherited at
  # clone time, but tofu still needs them in the resource attrs to detect
  # drift).
  mkContainer = name: meta:
    let
      isClone = meta.cloneFrom != null;
      # New: meta.image, when set on a container, overrides the default
      # NixOS LXC template. Used for non-NixOS LXCs (Debian developer
      # workstations, etc.) — pass the full vztmpl ref like
      # "local:vztmpl/debian-13-standard_13.1-2_amd64.tar.zst".
      hasCustomImage = (meta.image or null) != null && !isClone;
      lxcTemplateFile =
        if hasCustomImage then meta.image
        else nixosLxcTemplate;
      # Our NixOS LXC template → ostype "nixos" (INFRA-86). PVE 9 ships a
      # NixOS LXC setup plugin (PVE::LXC::Setup::NixOS) whose setup_network
      # writes a static /etc/systemd/network/eth0.network from the net0
      # ip=/gw= at create time — so a fresh container comes up on its
      # declared internal_ip with no separate bootstrap step at all. (Was
      # "unmanaged", under which PVE wrote no network config and the CT
      # fell back to DHCP.) Custom Debian/Ubuntu images keep their own
      # ostype; any other custom image stays "unmanaged".
      lxcOsType =
        if !hasCustomImage then "nixos"
        # A consumer's OWN NixOS template (e.g. a nixos-generators
        # proxmox-lxc image on local:) is still ostype nixos — PVE's
        # NixOS setup plugin then writes the static IP at create time,
        # same as the fleet template path.
        else if lib.hasInfix "nixos" lxcTemplateFile then "nixos"
        else if lib.hasInfix "debian" lxcTemplateFile then "debian"
        else if lib.hasInfix "ubuntu" lxcTemplateFile then "ubuntu"
        else "unmanaged";
      sourceConfig =
        if isClone then {
          clone = { vm_id = meta.cloneFrom; };
        } else {
          operating_system = {
            template_file_id = lxcTemplateFile;
            type = lxcOsType;
          };
        };
      # NixOS LXC templates already bake in the sysadmin SSH key via
      # nix/modules/infra/base/core/default.nix. Debian/Ubuntu templates don't —
      # inject via initialization.user_account so the operator can SSH
      # in immediately after first boot. Hostname is also set so the
      # LXC reports its name to DHCP / chrony / etc.
      # NixOS images (fleet template OR a consumer's own) bake their
      # operator key into the image; injecting user_account here too
      # would only create import-parity drift on existing containers.
      initConfig = lib.optionalAttrs (hasCustomImage && lxcOsType != "nixos") {
        initialization = {
          hostname = name;
          user_account = {
            keys = [ config.fleet.network.sysadmin_ssh_key ];
          };
        };
      };
      # bpg/proxmox creates a fresh empty volume when mount_point omits an
      # explicit volume reference. For cloned containers the data already
      # arrives via the clone, so re-declaring mount_point here would either
      # spawn a second empty disk (current behavior) or fight the cloned
      # one. Skip it on the clone path; declare the cloned mount via
      # ignore_changes so tofu doesn't try to reconcile.
      mountConfig =
        if isClone then {}
        else {
          mount_point = map (mp: {
            volume = mp.datastore;
            path = mp.path;
            size = mp.size;
            backup = mp.backup;
          }) meta.mount_points;
        };
      # ADR-047 moved the LXC template local: → nix-store:. For an EXISTING
      # container the `operating_system.template_file_id` is IMMUTABLE (the
      # creation template can't change without recreating the CT), so tofu reads
      # the new value as drift and ForceNew-replaces every template-path
      # container — blocked fleet-wide by prevent_destroy (was: "Instance cannot
      # be destroyed"). Ignore the whole operating_system block so applies stop
      # trying to destroy+recreate live containers. CREATE is unaffected (a fresh
      # CT still uses the current nix-store template); clones have no such block.
      #
      # `initialization` (hostname/dns/ip_config + user_account SSH keys) is the
      # same failure mode: it is ForceNew and the provider can't read it back from
      # a live CT, so `tofu import`/`tf adopt` sees the config's block as an ADD →
      # ForceNew replace → blocked by prevent_destroy. It is create-only data
      # (keys are injected once; changing them needs a recreate regardless), so
      # ignoring post-create drift is correct and makes state adoption a pure
      # operation. CREATE still emits it.
      #
      # `pool_id` is the same failure mode a third time: it is ForceNew on both
      # LXC and VM in bpg, so assigning an ALREADY-EXISTING guest to a pool
      # plans as destroy+recreate → blocked fleet-wide by prevent_destroy. A
      # guest carrying `pool` would otherwise make every apply of its stack
      # error. Ignore post-create drift on pool_id so pooled guests stay
      # appliable; membership for a running guest is set out-of-band once
      # (`pvesh set /pools/<id> -vms <vmid>`). CREATE still emits pool_id (a
      # fresh guest joins for free — but the pool must already exist, so declare
      # it in a stack applied no later than the guest's).
      lifecycleMeta =
        let base =
          if isClone then meta
          else meta // { ignore_changes = (meta.ignore_changes or []) ++ [ "operating_system" "initialization" ]; };
        in if meta.pool != null
           then base // { ignore_changes = (base.ignore_changes or []) ++ [ "pool_id" ]; }
           else base;
    in {
      provider = "proxmox.${builtins.elemAt (lib.strings.splitString "." meta.provider_instance) 1}";
      node_name = resolveNode meta;
      vm_id = meta.vm_id;
      started = meta.start_on_create;
      start_on_boot = meta.onboot;
      unprivileged = !meta.privileged;
      tags = meta.tags;

      cpu = { cores = meta.cpu_cores; }
        // lib.optionalAttrs (meta.arch != "amd64") { architecture = meta.arch; };
      memory = { dedicated = meta.memory_mb; swap = meta.swap_mb; };
      disk = { datastore_id = meta.root_disk_datastore; size = meta.root_disk_gb; };
      # Only emit fuse/keyctl/mknod/mount when on: a non-root PVE API token
      # may set `nesting` but 403s on ANY fuse/keyctl value (even false), so
      # omitting them when off lets the IaC path create token-managed CTs
      # (INFRA-88).
      features = {
        nesting = meta.features.nesting;
      } // (lib.optionalAttrs meta.features.fuse { fuse = true; })
        // (lib.optionalAttrs meta.features.keyctl { keyctl = true; })
        // (lib.optionalAttrs meta.features.mknod { mknod = true; })
        // (lib.optionalAttrs (meta.features.mount != []) { mount = meta.features.mount; });
      console = { type = "console"; };
    } // mountConfig // sourceConfig
      // (lifecycleAttrs meta)
      // (lib.optionalAttrs (meta.devices != []) {
        device_passthrough = map (d:
          { path = d.path; }
          // lib.optionalAttrs (d.uid != null) { uid = d.uid; }
          // lib.optionalAttrs (d.gid != null) { gid = d.gid; }
          // lib.optionalAttrs (d.mode != null) { mode = d.mode; }
          // lib.optionalAttrs d.deny_write { deny_write = true; }
        ) meta.devices;
      })
      // (lib.optionalAttrs (meta.hook_script != null) { hook_script_file_id = meta.hook_script; })
      // (lib.optionalAttrs (meta.pool != null) { pool_id = meta.pool; })
      // (let d = computeDescription meta; in lib.optionalAttrs (d != null) { description = d; })
      // (mkNetwork (meta // { _name = name; })) // (mkLifecycle lifecycleMeta)
      # initConfig is recursive-merged LAST so its initialization.user_account
      # joins mkNetwork's initialization.{hostname,dns,ip_config} instead of
      # being clobbered by Nix's shallow // operator.
      // (let merged = lib.recursiveUpdate
            (((mkNetwork (meta // { _name = name; })).initialization or {}))
            (initConfig.initialization or {});
          in lib.optionalAttrs hasCustomImage { initialization = merged; });

  # ── Raw lxc.conf lines (escape hatch) ─────────────────────────
  # terraform_data + local-exec over the same root-SSH channel the bpg
  # provider itself uses (nix/tf/providers/proxmox: ssh.username = root).
  # The block is rewritten idempotently between markers; the container is
  # rebooted only if running. `nix shell`, never `nix-shell -p` (flakes-only
  # operator hosts have no channels NIX_PATH).
  mkLxcExtraConf = name: meta:
    let
      inst = builtins.elemAt (lib.strings.splitString "." meta.provider_instance) 1;
      instCfg = config.fleet.providers.proxmox.${inst} or null;
      node = resolveNode meta;
      nodeAddr = if instCfg != null then (instCfg.cluster.node_addresses.${node} or node) else node;
      block = lib.concatStringsSep "\n" meta.lxc_extra_conf;
      vmid = toString meta.vm_id;
      script = ''
        set -e
        conf=/etc/pve/lxc/${vmid}.conf
        b='# BEGIN fleetkit lxc_extra_conf'
        e='# END fleetkit lxc_extra_conf'
        tmp=$(mktemp)
        awk -v b="$b" -v e="$e" '$0==b{skip=1} !skip{print} $0==e{skip=0}' "$conf" > "$tmp"
        { cat "$tmp"; echo "$b"; cat <<'FLEETKIT_BLOCK'
        ${block}
        FLEETKIT_BLOCK
        echo "$e"; } > "$conf"
        rm -f "$tmp"
        if pct status ${vmid} | grep -q running; then pct reboot ${vmid}; fi
      '';
    in {
      triggers_replace = [
        "\${proxmox_virtual_environment_container.${name}.id}"
        (builtins.hashString "sha256" block)
      ];
      provisioner = [{
        local-exec = {
          command = "nix shell nixpkgs#openssh --command ssh -o BatchMode=yes root@${nodeAddr} 'bash -s' <<'FLEETKIT_EOF'\n${script}FLEETKIT_EOF";
        };
      }];
    };

  # ── KVM/QEMU VM emitter ───────────────────────────────────────
  mkVm = name: meta:
    # cloud_init.enable = false: no cloud-init drive, no initialization
    # block at all (self-configuring appliance images). The key is removed
    # rather than nulled — a null nested block is not valid Terraform JSON.
    lib.removeAttrs (mkVmAttrs name meta) (lib.optional (!meta.cloud_init.enable) "initialization");

  mkVmAttrs = name: meta:
    let
      # ── New-style explicit image dispatch ──
      # If meta.image is set, parse it as either "file:<file_id>" or
      # "clone:<vmid>". Takes precedence over the legacy vm_template +
      # name-prefix dispatch below.
      imageStr = meta.image or null;
      imageParts = lib.optional (imageStr != null) (lib.splitString ":" imageStr);
      imageKind = if imageStr == null then null
                  else if lib.hasPrefix "file:" imageStr then "file"
                  else if lib.hasPrefix "clone:" imageStr then "clone"
                  else if lib.hasPrefix "import:" imageStr then "import"
                  else throw "mkVm(${name}): image must be \"file:...\", \"clone:<vmid>\" or \"import:<datastore>:import/<file>\", got ${imageStr}";
      imageFileId = if imageKind == "file"
                    then lib.removePrefix "file:" imageStr
                    else null;
      # "import:<datastore>:import/<file>" → disk.import_from (PVE 8.4+
      # import content type; the appliance-image path).
      imageImportFrom = if imageKind == "import"
                        then lib.removePrefix "import:" imageStr
                        else null;
      vmCfg = meta.vm;
      rootDiskExtra =
        lib.optionalAttrs (vmCfg.root_disk.cache != null) { cache = vmCfg.root_disk.cache; }
        // lib.optionalAttrs (vmCfg.root_disk.discard != null) { discard = vmCfg.root_disk.discard; }
        // lib.optionalAttrs (vmCfg.root_disk.iothread != null) { iothread = vmCfg.root_disk.iothread; }
        // lib.optionalAttrs (vmCfg.root_disk.ssd != null) { ssd = vmCfg.root_disk.ssd; };
      imageCloneVmId = if imageKind == "clone"
                       then lib.toInt (lib.removePrefix "clone:" imageStr)
                       else null;

      # ── Legacy name-prefix dispatch — kept for VMs not yet migrated
      # to explicit `image` + `cloud_init.users`. Gradual migration:
      # set `image` on a VM entry and the new path activates for it. ──
      isNetgate = name == "netgate";
      isHeadscaleRouter = name == "headscale-router";
      fromNixosTemplate = meta.vm_template == "nixos";
      isDept = lib.hasPrefix "dept-" name;
      isDev = lib.hasPrefix "dev-" name;

      # Whether to use the new image-dispatch path or the legacy one.
      newStyle = imageStr != null;
      declared = meta.network_mode == "declared";

      # Data disks → virtio1, virtio2, ... in declaration order.
      dataDiskEntries = lib.imap0 (i: dd: {
        interface = "virtio${toString (i + 1)}";
        datastore_id = dd.datastore_id;
        size = dd.size_gb;
      }) (meta.data_disks or []);

      # Legacy: hard-coded 128 GiB virtio1 for dev-* VMs that don't yet
      # declare data_disks. Drops out once dev-nithin migrates.
      legacyDevDataDisk =
        lib.optional (isDev && !newStyle && (meta.data_disks or []) == []) {
          interface = "virtio1"; datastore_id = ds; size = 128;
        };

      nicDefaults = {
        disconnected = false;
        enabled = true;
        firewall = false;
        mac_address = "";
        model = "virtio";
        mtu = 0;
        queues = 0;
        rate_limit = 0;
        trunks = "";
        vlan_id = 0;
      };
    in {
      provider = "proxmox.${builtins.elemAt (lib.strings.splitString "." meta.provider_instance) 1}";
      node_name = resolveNode meta;
      vm_id = meta.vm_id;
      name = name;
      tags = meta.tags;
      started = meta.start_on_create;

      cpu = { cores = meta.cpu_cores; type = vmCfg.cpu_type; };
      memory = { dedicated = meta.memory_mb; };
      operating_system = { type = "l26"; };

      disk = [(
        if newStyle then (
          # New-style: dispatch on imageKind. For "clone:<vmid>" the
          # root disk is just a sized entry (clone block provides the
          # source); "file:<file_id>" sets file_id; "import:…" sets
          # import_from.
          {
            interface = vmCfg.root_disk.interface; datastore_id = ds;
            size = meta.root_disk_gb;
          } // lib.optionalAttrs (imageKind == "file") { file_id = imageFileId; }
            // lib.optionalAttrs (imageKind == "import") { import_from = imageImportFrom; }
            // rootDiskExtra
        )
        else if fromNixosTemplate then {
          interface = vmCfg.root_disk.interface; datastore_id = ds; size = meta.root_disk_gb;
        } // rootDiskExtra
        else throw "mkVm(${name}): the legacy Debian-VM disk path was removed (INFRA-137). The fleet has no KVM Debian guests — use a Debian LXC (kind=container + image), or set an explicit `image = \"file:...\"`/`\"clone:...\"`."
      )] ++ dataDiskEntries ++ legacyDevDataDisk;

      network_device =
        if declared then map (n:
          nicDefaults // { bridge = bridgeFor n; model = n.model; firewall = n.firewall; }
          // nicCommon n) meta.interfaces
        else [
        (nicDefaults // {
          bridge = "vmbr0";
        } // lib.optionalAttrs isNetgate {
          mac_address = "BC:24:11:5B:EA:26";
        })
        (nicDefaults // {
          bridge = "vmbr1";
        } // lib.optionalAttrs isNetgate {
          mac_address = "BC:24:11:D0:E9:C0";
        })
      ];

      initialization = {
        datastore_id = if meta.cloud_init.datastore != null then meta.cloud_init.datastore else ds;
        type = "nocloud";
        dns = dnsFor meta;
      } // (
        # Declared NICs: ip_config mirrors `interfaces` one-to-one; the
        # cloud-init identity follows the image kind (a file: image gets
        # the per-VM snippet, everything else the sysadmin key).
        if declared then
          (if newStyle && imageKind == "file"
           then { user_data_file_id = "local:snippets/${name}-user-data.yaml"; }
           else { user_account = { username = "root"; keys = [ net.sysadmin_ssh_key ]; }; })
          // { ip_config = map ipConfigFor meta.interfaces; }
        else
        # New-style: a custom user-data snippet is generated by
        # nix/fleet/resources.nix's renderCloudInitSnippet, uploaded as
        # local:snippets/<name>-user-data.yaml. NixOS clone VMs in
        # new-style still get just the sysadmin key via user_account
        # (no custom snippet needed) — distinguished by imageKind.
        if newStyle then (
          if imageKind == "clone" then {
            user_account = { username = "root"; keys = [ net.sysadmin_ssh_key ]; };
            ip_config = [
              { ipv4 = { address = "dhcp"; }; }
              { ipv4 = { address = internalCidr meta.internal_ip; }; }
            ];
          } else {
            user_data_file_id = "local:snippets/${name}-user-data.yaml";
            ip_config = [
              { ipv4 = { address = "dhcp"; }; }
              { ipv4 = { address = internalCidr meta.internal_ip; }; }
            ];
          }
        )
        else if isNetgate then {
          user_account = { username = "root"; keys = [ net.sysadmin_ssh_key ]; };
          ip_config = [
            { ipv4 = { address = lanCidr meta.ip; } // optGw net.lan_gateway; }
            { ipv4 = { address = internalCidr meta.internal_ip; }; }
          ];
        }
        else if isHeadscaleRouter then {
          # Subnet router: static IPs on both NICs so it has direct L2
          # presence on each subnet it advertises (the LAN via eth0 on
          # vmbr0, the internal net via eth1 on vmbr1).
          #
          # Default gateway is the LAN router on eth0, NOT netgate on
          # eth1: vmbr1's L2 between this VM and netgate is flaky
          # (probably the same bridge-bleed issue that confuses ARP
          # between vmbr0 and vmbr1 — packets between the headscale-
          # router and netgate's vmbr1 interface keep getting dropped).
          # The LAN router has real internet upstream anyway, and
          # netgate also uses it as its own default gateway.
          #
          # WAN-side address is pinned via
          # fleet.settings.network.staticWanCidrs (not meta.ip) so the
          # fleet entry can keep `ip = ""` — that makes the colmena
          # targetHost resolve to `internal_ip`. SSH from PVE to the
          # WAN address fails because netgate apparently bridges
          # vmbr0↔vmbr1 at L2 (eth1's MAC keeps showing up in PVE's
          # vmbr0 ARP table for the WAN IP), so colmena must reach the
          # host via vmbr1.
          user_account = { username = "root"; keys = [ net.sysadmin_ssh_key ]; };
          ip_config = [
            { ipv4 = {
                address = config.fleet.settings.network.staticWanCidrs.${name} or
                  (throw "mkVm(${name}): legacy headscale-router VMs need a WAN-side CIDR in fleet.settings.network.staticWanCidrs.\"${name}\"");
              } // optGw net.lan_gateway; }
            { ipv4 = { address = internalCidr meta.internal_ip; }; }
          ];
        }
        else if fromNixosTemplate then {
          # Generic NixOS-template VM (e.g. sysadmin workstation, ADR-017).
          # Cloud-init only seeds the sysadmin root SSH key — the host's
          # NixOS config (Colmena) creates user accounts and services.
          user_account = { username = "root"; keys = [ net.sysadmin_ssh_key ]; };
          ip_config = [
            { ipv4 = { address = "dhcp"; }; }
            { ipv4 = { address = internalCidr meta.internal_ip; }; }
          ];
        }
        else if isDept then {
          user_account = { username = "root"; keys = [ net.sysadmin_ssh_key ]; };
          ip_config = [
            { ipv4 = { address = "dhcp"; }; }
            { ipv4 = { address = internalCidr meta.internal_ip; }; }
          ];
        }
        else if isDev then {
          # Per-VM cloud-init snippet uploaded as kind="file" in
          # nix/fleet/resources.nix. The snippet does per-VM finalisation
          # (hostname, dev user, /data mount, Nix install) on top of the
          # image-baked Debian (docker, qemu-guest-agent, dev tools, see
          # ADR-018). See ADR-013.
          user_data_file_id = "local:snippets/${name}-user-data.yaml";
          ip_config = [
            { ipv4 = { address = "dhcp"; }; }
            { ipv4 = { address = internalCidr meta.internal_ip; }; }
          ];
        }
        else throw "mkVm: unknown VM class for ${name}"
      );

      # Agent enabled when the OS has qemu-guest-agent installed.
      # New-style: always on (prepared images bake it in; clone-from-template
      # VMs ship NixOS which also runs it). Legacy: only dev/nixos paths had it.
      # vm.agent overrides the rule either way.
      agent = { enabled = if vmCfg.agent != null then vmCfg.agent else (newStyle || isDev || fromNixosTemplate); };
    }
    # A serial socket so `qm terminal <vmid>` can reach the guest console —
    # essential for debugging stuck boots on non-NixOS VMs (firstboot
    # prompts, cloud-init failures, kernel panics). On by default; pattern is
    # the community-scripts default.
    // lib.optionalAttrs vmCfg.serial_console { serial_device = [{ device = "socket"; }]; }
    // lib.optionalAttrs (vmCfg.machine != null) { machine = vmCfg.machine; }
    // lib.optionalAttrs (vmCfg.bios != null) { bios = vmCfg.bios; }
    // lib.optionalAttrs (vmCfg.bios == "ovmf") {
      efi_disk = {
        datastore_id = if vmCfg.efi.datastore != null then vmCfg.efi.datastore else ds;
        file_format = "raw";
        type = vmCfg.efi.type;
        pre_enrolled_keys = vmCfg.efi.pre_enrolled_keys;
      };
    }
    // lib.optionalAttrs (vmCfg.scsi_hardware != null) { scsi_hardware = vmCfg.scsi_hardware; }
    // lib.optionalAttrs (vmCfg.boot_order != null) { boot_order = vmCfg.boot_order; }
    // lib.optionalAttrs (vmCfg.tablet != null) { tablet_device = vmCfg.tablet; }
    // (lifecycleAttrs meta)
    // lib.optionalAttrs (!meta.onboot) { on_boot = false; }
    // lib.optionalAttrs (newStyle && imageKind == "clone") {
      clone = { vm_id = imageCloneVmId; datastore_id = ds; full = true; }
        // lib.optionalAttrs (meta ? cloneFrom && false) {};  # placeholder for cross-host clones
    }
    // lib.optionalAttrs (!newStyle && fromNixosTemplate) {
      clone = { vm_id = 9000; datastore_id = ds; full = true; };
    }
    // lib.optionalAttrs (meta.pool != null) { pool_id = meta.pool; }
    // (let d = computeDescription meta; in lib.optionalAttrs (d != null) { description = d; })
    # pool_id is ForceNew (see the LXC emitter's lifecycleMeta note): ignore
    # post-create drift so a pooled VM stays appliable and is never recreated
    # just to change pool membership.
    // (mkLifecycle (if meta.pool != null
                     then meta // { ignore_changes = (meta.ignore_changes or []) ++ [ "pool_id" ]; }
                     else meta));

  # ── Non-OS resource emitters ──────────────────────────────────
  mkBridge = name: meta: {
    provider = "proxmox.${builtins.elemAt (lib.strings.splitString "." meta.provider_instance) 1}";
    node_name = resolveNode meta;
    name = name;
    ports = meta.ports or [];
    address = meta.address or null;
    autostart = meta.autostart or true;
    comment = meta.comment or "";
  } // (mkLifecycle meta);

  mkPool = name: meta: {
    provider = "proxmox.${builtins.elemAt (lib.strings.splitString "." meta.provider_instance) 1}";
    pool_id = meta.pool_id;
    comment = meta.comment or "";
  } // (mkLifecycle meta);

  # Cluster-wide PVE group. Pre-declared so ACLs (mkAcl) have a group to
  # bind to on a fresh cluster — PVE rejects an ACL whose group_id doesn't
  # yet exist. For Authentik-synced groups the OIDC realm's groups-autocreate
  # would also create them, but only at first login; declaring them here makes
  # the ACL apply order-independent (INFRA-130 / ADR-071). Membership stays
  # IdP-driven (computed `members`, not managed here).
  mkGroup = name: meta: {
    provider = "proxmox.${builtins.elemAt (lib.strings.splitString "." meta.provider_instance) 1}";
    group_id = meta.group_id;
    comment = meta.comment or "";
  } // (mkLifecycle meta);

  # `depends_on` (optional): list of TF resource addresses, e.g.
  # [ "proxmox_virtual_environment_group.platform-admins-group" ]. Used to
  # force group-before-ACL ordering within a single apply, since group_id is
  # a literal string and creates no implicit dependency edge (INFRA-130).
  mkAcl = name: meta: {
    provider = "proxmox.${builtins.elemAt (lib.strings.splitString "." meta.provider_instance) 1}";
    path = meta.path;
    role_id = meta.role_id;
    group_id = meta.group_id or null;
    user_id = meta.user_id or null;
    token_id = meta.token_id or null;
    propagate = meta.propagate or true;
  } // (lib.optionalAttrs (meta ? depends_on && meta.depends_on != [])
         { depends_on = meta.depends_on; })
    // (mkLifecycle meta);

  mkDns = name: meta: {
    provider = "proxmox.${builtins.elemAt (lib.strings.splitString "." meta.provider_instance) 1}";
    node_name = resolveNode meta;
    domain = meta.domain;
    servers = meta.servers;
  } // (mkLifecycle meta);

  mkDownload = name: meta: {
    provider = "proxmox.${builtins.elemAt (lib.strings.splitString "." meta.provider_instance) 1}";
    node_name = resolveNode meta;
    datastore_id = meta.datastore_id;
    content_type = meta.content_type;
    url = meta.url;
    file_name = meta.file_name;
    overwrite = meta.overwrite or false;
  } // (lib.optionalAttrs (meta ? upload_timeout) { upload_timeout = meta.upload_timeout; })
    // (mkLifecycle meta);

  # File resource. Two source modes:
  #   • inline string content (default) — emits `source_raw`. Used for
  #     cloud-init snippets and hookscripts.
  #   • binary file (set meta.source = "nixos-lxc-image") — emits
  #     `source_file` pointing at a nix-built image. The bpg provider SCPs
  #     the file up to PVE on apply.
  # (The "debian-cloud-image" source was removed with the Debian-VM path —
  #  INFRA-137.)
  mkFile = name: meta:
    let
      sourceBlock =
        if (meta.source or null) == "nixos-lxc-image" then
          assert lib.assertMsg (preparedNixosLxcTemplatePath != null)
            "mkFile: meta.source=\"nixos-lxc-image\" requires pkgs and fleetLib in scope (the images-family builder).";
          { source_file = [{
              path = preparedNixosLxcTemplatePath;
              file_name = meta.file_name;
            }];
          }
        else
          { source_raw = [{
              data = meta.data;
              file_name = meta.file_name;
              resize = 0;
            }];
          };
    in {
      provider = "proxmox.${builtins.elemAt (lib.strings.splitString "." meta.provider_instance) 1}";
      node_name = resolveNode meta;
      datastore_id = meta.datastore_id;
      content_type = meta.content_type;
      overwrite = meta.overwrite or true;
    } // sourceBlock
      # `depends_on` (optional), same escape hatch and same reason as mkAcl:
      # datastore_id is a literal string, so uploading into a datastore this
      # stack also CREATES produces no dependency edge, and terraform is free
      # to run both at once. The upload then fails on a storage that does not
      # exist yet and the apply has to be repeated. A single-node fleet hits
      # this on its very first apply, where the template datastore and the
      # template land together.
      // (lib.optionalAttrs (meta ? depends_on && meta.depends_on != [])
           { depends_on = meta.depends_on; })
      // (mkLifecycle meta);

  # External metric server (cluster-wide). PVE pushes node/guest/storage
  # stats to it. We use the OpenTelemetry plugin (OTLP/HTTP) → the Alloy
  # gateway on the otel CT (INFRA-47 / ADR-038). Only proto + path are set;
  # the bpg provider doesn't yet expose headers/TLS-verify (issue #2594),
  # which is why the gateway listens unauthenticated on the internal net.
  mkMetricsServer = name: meta: {
    provider = "proxmox.${builtins.elemAt (lib.strings.splitString "." meta.provider_instance) 1}";
    name = meta.server_name or name;
    server = meta.server;
    port = meta.port;
    type = "opentelemetry";
    opentelemetry_proto = meta.opentelemetry_proto or "http";
    opentelemetry_path = meta.opentelemetry_path or "/v1/metrics";
  } // lib.optionalAttrs (meta ? disable) { disable = meta.disable; }
    // (mkLifecycle meta);

  # ── PVE SDN (zone → vnet → subnet) ─────────────────────────────
  # bpg names: proxmox_sdn_zone_{simple,vlan}, proxmox_sdn_vnet,
  # proxmox_sdn_subnet, proxmox_sdn_applier (the older
  # proxmox_virtual_environment_sdn_* names are deprecated, same story as
  # cluster-options). Confirm them against the pinned provider with
  # `tofu providers schema -json` on first use.
  mkSdnZone = name: meta: {
    provider = "proxmox.${builtins.elemAt (lib.strings.splitString "." meta.provider_instance) 1}";
    id = meta.zone_id or name;
  } // lib.optionalAttrs (meta ? nodes) { nodes = meta.nodes; }
    // lib.optionalAttrs (meta ? mtu) { mtu = meta.mtu; }
    // lib.optionalAttrs (meta ? dns_zone) { dns_zone = meta.dns_zone; }
    // lib.optionalAttrs (meta ? ipam) { ipam = meta.ipam; }
    // lib.optionalAttrs ((meta.zone_type or "simple") == "vlan") { bridge = meta.bridge; }
    // (mkLifecycle meta);

  mkSdnVnet = name: meta: {
    provider = "proxmox.${builtins.elemAt (lib.strings.splitString "." meta.provider_instance) 1}";
    id = meta.vnet_id or name;
    zone = meta.zone;
  } // lib.optionalAttrs (meta ? tag) { tag = meta.tag; }
    // lib.optionalAttrs (meta ? vlan_aware) { vlan_aware = meta.vlan_aware; }
    // lib.optionalAttrs (meta ? alias) { alias = meta.alias; }
    // lib.optionalAttrs (meta ? isolate_ports) { isolate_ports = meta.isolate_ports; }
    // (mkLifecycle meta);

  mkSdnSubnet = name: meta: {
    provider = "proxmox.${builtins.elemAt (lib.strings.splitString "." meta.provider_instance) 1}";
    vnet = meta.vnet;
    cidr = meta.cidr;
  } // lib.optionalAttrs (meta ? gateway) { gateway = meta.gateway; }
    // lib.optionalAttrs (meta ? snat) { snat = meta.snat; }
    // lib.optionalAttrs (meta ? dhcp_range) { dhcp_range = meta.dhcp_range; }
    // lib.optionalAttrs (meta ? dhcp_dns_server) { dhcp_dns_server = meta.dhcp_dns_server; }
    // (mkLifecycle meta);

  # One applier per (stack, provider instance): SDN changes are staged in
  # PVE until applied; this resource applies them and re-runs whenever any
  # SDN resource of the stack changes.
  mkSdnApplier = instance: sdnAddrs: {
    provider = "proxmox.${instance}";
    depends_on = sdnAddrs;
    lifecycle = [{ replace_triggered_by = sdnAddrs; }];
  };

  # Node VLAN interface (e.g. vmbr0.42) — replaces the manual
  # /etc/network/interfaces edit the community bridge picker assumed.
  mkLinuxVlan = name: meta: {
    provider = "proxmox.${builtins.elemAt (lib.strings.splitString "." meta.provider_instance) 1}";
    node_name = resolveNode meta;
    name = meta.interface_name or name;
  } // lib.optionalAttrs (meta ? interface) { interface = meta.interface; }
    // lib.optionalAttrs (meta ? vlan) { vlan = meta.vlan; }
    // lib.optionalAttrs (meta ? address) { address = meta.address; }
    // lib.optionalAttrs (meta ? gateway) { gateway = meta.gateway; }
    // lib.optionalAttrs (meta ? mtu) { mtu = meta.mtu; }
    // lib.optionalAttrs (meta ? comment) { comment = meta.comment; }
    // (mkLifecycle meta);

  # Cluster storages — the declarative twin of storage-share-helper.sh
  # (NFS) and of adding a directory storage in the UI. `content` lists PVE
  # content types (images, rootdir, vztmpl, iso, snippets, backup, import).
  mkStorageNfs = name: meta: {
    provider = "proxmox.${builtins.elemAt (lib.strings.splitString "." meta.provider_instance) 1}";
    id = meta.storage_id or name;
    server = meta.server;
    export = meta.export;
    content = meta.content or [ "images" "rootdir" ];
  } // lib.optionalAttrs (meta ? nodes) { nodes = meta.nodes; }
    // lib.optionalAttrs (meta ? options) { options = meta.options; }
    // lib.optionalAttrs (meta ? shared) { shared = meta.shared; }
    // lib.optionalAttrs (meta ? disable) { disable = meta.disable; }
    // (mkLifecycle meta);

  mkStorageDir = name: meta: {
    provider = "proxmox.${builtins.elemAt (lib.strings.splitString "." meta.provider_instance) 1}";
    id = meta.storage_id or name;
    path = meta.path;
    content = meta.content or [ "images" "rootdir" "vztmpl" "iso" "snippets" "backup" ];
  } // lib.optionalAttrs (meta ? nodes) { nodes = meta.nodes; }
    // lib.optionalAttrs (meta ? shared) { shared = meta.shared; }
    // lib.optionalAttrs (meta ? disable) { disable = meta.disable; }
    // (mkLifecycle meta);

  # Datacenter-level options (singleton per cluster). The only field
  # we currently set is `description`, rendered from the structured
  # `note` attribute by templates/proxmox/mkDatacenterNote.
  mkClusterOptions = name: meta:
    let
      base = {
        provider = "proxmox.${builtins.elemAt (lib.strings.splitString "." meta.provider_instance) 1}";
        # Set keyboard explicitly to match the bpg/proxmox provider's default.
        # Otherwise the provider injects "en-us" on read and tofu detects
        # this as drift on every plan, forcing destroy + recreate cycles.
        keyboard = meta.keyboard or "en-us";
      } // lib.optionalAttrs (meta ? note && meta.note != null) {
        description = notes.mkDatacenterNote meta.note;
      };
      # Provider returns `description` with different markdown-link escaping
      # than was sent → "Provider produced inconsistent result after apply".
      # Force-merge `ignore_changes = [ "description" ]` into the lifecycle
      # block on top of whatever mkLifecycle already emits (protect, etc.).
      metaWithIgnore = meta // {
        ignore_changes = (meta.ignore_changes or []) ++ [ "description" ];
      };
    in base // mkLifecycle metaWithIgnore;
}
