# fleet.guests.<estate>.<name>: Proxmox guests, shaped after the bpg/proxmox
# 0.115.0 resources they render to. id "<estate>/<name>".
#
#   kind = "lxc" -> proxmox_virtual_environment_container
#   kind = "vm"  -> proxmox_virtual_environment_vm
#
# Every modelled field names the provider argument it feeds in its
# description and in docs/guest-provider-map.md (checked against the provider
# schema by tests/guest_fidelity.py). docs/guest-model.md has the decisions.
#
# Layering, lowest to highest (null = silent at every layer; attrsets merge
# per key, scalars and lists replace):
#   1. schema defaults (lxc: unprivileged = true, features.nesting = true)
#   2. fleet.sites.<site>.providers.proxmox.defaults of the guest's site
#      (console, startOnBoot, started, lxc operating system, first NIC name
#      and bridge)
#   3. fleet.estates.<estate>.guestDefaults (the knob set below; knobs that
#      do not exist for the guest's kind are skipped, not errors)
#   4. the guest itself
# The effective result is published read-only, in the provider's own
# argument names, at fleet.report.providerView.<estate>.<guest>.
#
# Derived, read-only per guest: id, ipv4.<network> (from networkInterfaces;
# config.nix reads guests.<e>.<g>.ipv4.lan) and effectivePool (the guest's
# pool, else the estate's placement.pool).
#
# Also owned here: fleet.report.ipCollisions (data, never an error),
# fleet.report.vmidCollisions + fleet.allow.vmidCollisions (a vmid used
# twice in one cluster fails unless allowlisted with a reason),
# fleet.report.guestOptionPaths (every modelled guest option path, for the
# fidelity test) and fleet.estates.<estate>.guestDefaults.
{ lib, config, ... }:
let
  inherit (lib)
    types
    mkOption
    concatMap
    concatLists
    mapAttrsToList
    attrValues
    filter
    optional
    optionals
    elem
    imap0
    length
    ;
  h = import ./lib.nix { inherit lib; };
  cfg = config.fleet;
  inherit (cfg.report) ids;

  substrateRoles = [
    "dns"
    "ca"
    "cache"
    "identity"
    "ingress"
    "observability"
    "secretsEngine"
  ];

  # ---------------------------------------------------------------- types --
  octet = "(25[0-5]|2[0-4][0-9]|1[0-9][0-9]|[1-9]?[0-9])";
  ipv4Re = "(${octet}\\.){3}${octet}";
  ipv4 = types.strMatching ipv4Re;
  macAddress = types.strMatching "([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}";
  # GiB as an integer, or a provider size string such as "512G" / "4T".
  diskSize = types.either types.ints.positive (types.strMatching "[1-9][0-9]*[MGT]");

  # Read-only derived views only (never set by config): a JSON value.
  jsonValue =
    types.nullOr (
      types.oneOf [
        types.bool
        types.int
        types.float
        types.str
        (types.listOf jsonValue)
        (types.attrsOf jsonValue)
      ]
    )
    // {
      description = "JSON value (derived, read-only)";
    };

  nullable =
    type: description:
    mkOption {
      type = types.nullOr type;
      default = null;
      inherit description;
    };

  # ------------------------------------------------------------ knob set --
  # Shared by fleet.guests.<e>.<g> and fleet.estates.<e>.guestDefaults.
  # Descriptions name the provider argument (container / vm).
  knobOptions = {
    description = nullable types.str "description (both kinds).";
    tags = nullable (types.listOf types.str) "tags (both kinds). Lists replace, never merge.";
    pool = nullable types.str "Pool id (<estate>/proxmox/<pool>) -> pool_id. null = the estate's placement.pool; see effectivePool.";
    protect = nullable types.bool "Terraform lifecycle.prevent_destroy (meta-argument, not a provider argument). Effective default false.";
    protection = nullable types.bool "protection (both kinds): the Proxmox protection flag.";
    ignoreChanges = nullable (types.listOf types.str) "Terraform lifecycle.ignore_changes (meta-argument): provider argument names whose drift is ignored after create.";
    startOnBoot = nullable types.bool "container start_on_boot / vm on_boot.";
    started = nullable types.bool "started (both kinds).";
    startup = {
      order = nullable types.ints.unsigned "startup.order (both kinds).";
      upDelay = nullable types.ints.unsigned "startup.up_delay (both kinds), seconds.";
      downDelay = nullable types.ints.unsigned "startup.down_delay (both kinds), seconds.";
    };
    cpu = {
      cores = nullable types.ints.positive "cpu.cores (both kinds).";
      units = nullable types.ints.positive "cpu.units (both kinds).";
      limit = nullable types.ints.unsigned "cpu.limit (both kinds); 0 = unlimited.";
      architecture = nullable (types.enum [
        "amd64"
        "arm64"
        "armhf"
        "i386"
        "x86_64"
        "aarch64"
      ]) "cpu.architecture (both kinds).";
      type = nullable types.str "vm only: cpu.type (e.g. host).";
    };
    memory = {
      dedicatedMiB = nullable types.ints.positive "memory.dedicated (both kinds), MiB.";
      swapMiB = nullable types.ints.unsigned "lxc only: memory.swap, MiB.";
      floatingMiB = nullable types.ints.unsigned "vm only: memory.floating (balloon), MiB.";
    };
    features = {
      nesting = nullable types.bool "lxc only: features.nesting. Schema default true.";
      keyctl = nullable types.bool "lxc only: features.keyctl.";
      fuse = nullable types.bool "lxc only: features.fuse.";
      mount = nullable (types.listOf (types.enum [
        "nfs"
        "cifs"
      ])) "lxc only: features.mount.";
    };
    unprivileged = nullable types.bool "lxc only: unprivileged. Schema default true.";
    console.type = nullable (types.enum [
      "console"
      "tty"
      "shell"
    ]) "lxc only: console.type. Default from providers.proxmox.defaults.console.";
    environment = nullable (types.attrsOf types.str) "lxc only: environment_variables.";
    defaultDatastore = nullable types.str "Storage id used by every disk whose datastore is null (layering helper; feeds disk datastore_id / mount_point volume).";
    initialization = {
      hostname = nullable types.str "lxc only: initialization.hostname. Effective default: the guest name.";
      dns = {
        servers = nullable (types.listOf types.str) "initialization.dns.servers (both kinds).";
        domain = nullable types.str "initialization.dns.domain (both kinds).";
      };
      userAccount = {
        keys = nullable (types.listOf types.str) "initialization.user_account.keys (both kinds) as REFERENCES \"<principal>/ssh/<label>\" to principal public keys; resolved to the public key text in providerView. Passwords are never modelled.";
        username = nullable types.str "vm only: initialization.user_account.username (cloud-init user).";
      };
    };
    source = {
      osType = nullable types.str "operating_system.type (lxc: nixos/debian/...; vm: l26/...). lxc default from providers.proxmox.defaults.operatingSystem.type.";
      template = nullable types.str "lxc only: image id (<site>/<image>) -> operating_system.template_file_id = that image's volumeId. null = providers.proxmox.defaults.operatingSystem.templateFileId.";
      clone = nullable (types.submodule {
        options = {
          vmid = mkOption {
            type = types.ints.positive;
            description = "clone.vm_id (both kinds): source VMID (a template, need not be a modelled guest).";
          };
          datastore = nullable types.str "clone.datastore_id (both kinds): storage id.";
          full = nullable types.bool "clone.full (both kinds).";
          node = nullable types.str "clone.node_name (both kinds): node id holding the source.";
        };
      }) "Clone source instead of a template (lxc: replaces operating_system).";
      cdrom = nullable (types.submodule {
        options = {
          image = mkOption {
            type = types.str;
            description = "vm only: image id (contentType iso) -> cdrom.file_id = its volumeId.";
          };
          interface = nullable types.str "vm only: cdrom.interface (e.g. ide2).";
        };
      }) "vm only: ISO install medium.";
    };
    vm = {
      agent = nullable types.bool "vm only: agent.enabled (qemu guest agent).";
      bios = nullable (types.enum [
        "seabios"
        "ovmf"
      ]) "vm only: bios.";
      machine = nullable types.str "vm only: machine (e.g. q35).";
      serialDevices = nullable (types.listOf types.str) "vm only: serial_device[].device (e.g. socket).";
      cloudInit = {
        datastore = nullable types.str "vm only: initialization.datastore_id (storage id of the cloud-init drive).";
        type = nullable (types.enum [
          "nocloud"
          "configdrive2"
        ]) "vm only: initialization.type.";
      };
    };
  };
  knobNames = builtins.attrNames knobOptions;

  # Knob paths that exist for one kind only.
  lxcOnlyKnobs = [
    [ "unprivileged" ]
    [ "features" ]
    [ "console" ]
    [ "memory" "swapMiB" ]
    [ "environment" ]
    [ "source" "template" ]
    [ "initialization" "hostname" ]
  ];
  vmOnlyKnobs = [
    [ "memory" "floatingMiB" ]
    [ "cpu" "type" ]
    [ "vm" ]
    [ "source" "cdrom" ]
    [ "initialization" "userAccount" "username" ]
  ];
  foreignKnobs = kind: if kind == "lxc" then vmOnlyKnobs else lxcOnlyKnobs;

  # ----------------------------------------------------- list item types --
  interfaceType = types.submodule {
    options = {
      name = nullable types.str "lxc: network_interface.name. Default: providers.proxmox.defaults.nic.name for the first interface, eth<i> after. (vm network_device has no name.)";
      network = mkOption {
        type = types.str;
        description = "Network NAME of the guest's site (e.g. lan). Keys ipv4.<network>; supplies the prefix and the gateway defaults. Model-only.";
      };
      bridge = nullable types.str "Bridge id (<site>/<bridge>) -> network_interface.bridge / network_device.bridge (the bridge name). Default providers.proxmox.defaults.nic.bridge.";
      vlanId = nullable (types.ints.between 1 4094) "network_interface.vlan_id / network_device.vlan_id.";
      macAddress = nullable macAddress "network_interface.mac_address / network_device.mac_address.";
      firewall = nullable types.bool "network_interface.firewall / network_device.firewall.";
      rateLimit = nullable types.number "network_interface.rate_limit / network_device.rate_limit (MB/s).";
      mtu = nullable types.ints.positive "network_interface.mtu / network_device.mtu.";
      model = nullable (types.enum [
        "virtio"
        "e1000"
        "rtl8139"
        "vmxnet3"
      ]) "vm only: network_device.model.";
      address = nullable (types.either ipv4 (types.enum [ "dhcp" ])) "IPv4 address WITHOUT prefix, or \"dhcp\" -> initialization.ip_config[i].ipv4.address. null = no IPv4 config.";
      prefix = nullable (types.ints.between 0 32) "Prefix length appended to address. Default: the network's cidr prefix.";
      gateway = nullable ipv4 "initialization.ip_config[i].ipv4.gateway. Default (first interface only): the address of the network's gateway router.";
    };
  };

  diskType = types.submodule {
    options = {
      role = mkOption {
        type = types.enum [
          "root"
          "mount"
          "data"
        ];
        description = "root: lxc disk{} / the vm boot disk; mount: lxc mount_point{}; data: further vm disk{} entries.";
      };
      datastore = nullable types.str "Storage id -> disk.datastore_id (lxc root, vm) / mount_point.volume (lxc mount). null = defaultDatastore.";
      size = nullable diskSize "GiB (int) or \"<n>M|G|T\" -> lxc disk.size / vm disk.size (GiB number), mount_point.size (string).";
      path = nullable types.str "lxc mount only: mount_point.path inside the container.";
      backup = nullable types.bool "lxc mount: mount_point.backup; vm: disk.backup.";
      readOnly = nullable types.bool "lxc mount only: mount_point.read_only.";
      quota = nullable types.bool "lxc only: disk.quota / mount_point.quota.";
      shared = nullable types.bool "lxc mount only: mount_point.shared.";
      mountOptions = nullable (types.listOf types.str) "lxc only: disk.mount_options / mount_point.mount_options.";
      interface = nullable (types.strMatching "(ide|sata|scsi|virtio)[0-9]+") "vm only (required there): disk.interface.";
      discard = nullable (types.enum [
        "on"
        "ignore"
      ]) "vm only: disk.discard.";
      iothread = nullable types.bool "vm only: disk.iothread.";
      ssd = nullable types.bool "vm only: disk.ssd.";
      fileFormat = nullable (types.enum [
        "raw"
        "qcow2"
        "vmdk"
      ]) "vm only: disk.file_format.";
      cache = nullable types.str "vm only: disk.cache.";
    };
  };
  lxcOnlyDiskFields = [
    "path"
    "readOnly"
    "quota"
    "shared"
    "mountOptions"
  ];
  vmOnlyDiskFields = [
    "interface"
    "discard"
    "iothread"
    "ssd"
    "fileFormat"
    "cache"
  ];

  devicePassthroughType = types.submodule {
    options = {
      path = mkOption {
        type = types.strMatching "/dev/.+";
        description = "lxc only: device_passthrough.path (host device node).";
      };
      pcie = nullable types.str "pcie device id (<site>/<node>/<dev>) this device node belongs to (provenance, validated: must be on the guest's node). Model-only.";
      uid = nullable types.ints.unsigned "device_passthrough.uid.";
      gid = nullable types.ints.unsigned "device_passthrough.gid.";
      mode = nullable (types.strMatching "0?[0-7]{3,4}") "device_passthrough.mode.";
      denyWrite = nullable types.bool "device_passthrough.deny_write.";
    };
  };

  hostpciType = types.submodule {
    options = {
      device = nullable (types.strMatching "hostpci[0-9]+") "vm only: hostpci.device. Default hostpci<i>.";
      pcie = mkOption {
        type = types.str;
        description = "pcie device id (<site>/<node>/<dev>) -> hostpci.id = that device's pci address. Must be on the guest's node.";
      };
      pcieExpress = nullable types.bool "hostpci.pcie.";
      rombar = nullable types.bool "hostpci.rombar.";
      xvga = nullable types.bool "hostpci.xvga.";
    };
  };

  # ---------------------------------------------------------- guest type --
  guestType =
    estate:
    types.submodule (
      { name, config, ... }:
      {
        options = knobOptions // {
          id = h.mkId (h.qualify estate name);
          vmid = mkOption {
            type = types.ints.between 100 999999999;
            description = "vm_id (both kinds). Unique per cluster unless allowlisted.";
          };
          kind = mkOption {
            type = types.enum [
              "lxc"
              "vm"
            ];
            description = "lxc -> proxmox_virtual_environment_container, vm -> proxmox_virtual_environment_vm.";
          };
          on = mkOption {
            type = types.str;
            description = "Node id (<site>/<node>) -> node_name (the node name). Its site's proxmox provider supplies the defaults.";
          };
          hostKeys.age = nullable (types.strMatching "age1[02-9ac-hj-np-z]{58}") "Public age recipient (age1...) of the guest. Becomes its sops recipient when it is a reader of a secrets file; use it when adopting an existing .sops.yaml. Takes precedence over hostKeys.ed25519 for the recipient; never feeds a provider argument.";
          hostKeys.ed25519 = nullable (types.strMatching "ssh-ed25519 AAAA[A-Za-z0-9+/]+={0,2}( [^\n]*)?") "Public ssh ed25519 host key (ssh-ed25519 AAAA..., a trailing comment is accepted and dropped). Becomes the guest's sops recipient when it is a reader of a secrets file; never feeds a provider argument.";
          nixos.module = mkOption {
            type = types.nullOr types.path;
            default = null;
            description = "The NixOS module that says what runs in the guest (a file in the declaring repo). null = none declared. Not a provider argument.";
          };
          mode = mkOption {
            type = types.enum [
              "managed"
              "adopted"
              "planned"
            ];
            default = "managed";
            description = "managed: fleet owns it; adopted: exists, fleet observes and must not recreate; planned: not created yet. Model-only.";
          };
          substrate = mkOption {
            type = types.listOf (types.enum substrateRoles);
            default = [ ];
            description = "Substrate roles this guest plays; must match the estate's substrate lists. Model-only.";
          };
          networkInterfaces = mkOption {
            type = types.listOf interfaceType;
            default = [ ];
            description = "lxc network_interface[] / vm network_device[], in order; addresses feed initialization.ip_config[] at the same index.";
          };
          disks = mkOption {
            type = types.listOf diskType;
            default = [ ];
            description = "lxc: exactly one role=root (disk{}) plus role=mount entries (mount_point[]); vm: exactly one role=root plus role=data entries (disk[], each with an interface).";
          };
          devicePassthrough = mkOption {
            type = types.listOf devicePassthroughType;
            default = [ ];
            description = "lxc only: device_passthrough[].";
          };
          hostpci = mkOption {
            type = types.listOf hostpciType;
            default = [ ];
            description = "vm only: hostpci[] (PCI passthrough of node pcie devices).";
          };
          lxcExtraConf = nullable (types.listOf types.str) "lxc only: raw lines appended verbatim to /etc/pve/lxc/<vmid>.conf. NOT a provider argument: rendered as the terraform_data.<guest>-lxc-conf companion.";

          ipv4 = mkOption {
            type = types.attrsOf ipv4;
            readOnly = true;
            default = builtins.listToAttrs (
              map (n: {
                name = n.network;
                value = n.address;
              }) (filter (n: n.address != null && n.address != "dhcp") config.networkInterfaces)
            );
            description = "Derived, read-only: static IPv4 address per network NAME, from networkInterfaces.";
          };
          effectivePool = mkOption {
            type = types.nullOr types.str;
            readOnly = true;
            default = (effective estate config).pool;
            description = "Derived, read-only: pool id after layering (guest pool, guestDefaults pool, else fleet.estates.<estate>.placement.pool) -> pool_id.";
          };
        };
      }
    );

  # guests.<estate> is a container of typed guests, not a loose block.
  estateGuestsType = types.submodule (
    { name, ... }:
    {
      freeformType = types.lazyAttrsOf (guestType name);
    }
  );

  # -------------------------------------------------------------- layers --
  # Right-biased merge where null is silent; attrsets merge per key.
  over =
    a: b:
    if b == null then
      a
    else if a == null then
      b
    else if builtins.isAttrs a && builtins.isAttrs b then
      lib.zipAttrsWith (
        _: vs: if length vs == 1 then builtins.head vs else over (builtins.elemAt vs 0) (builtins.elemAt vs 1)
      ) [ a b ]
    else
      b;

  knobsOf = x: lib.getAttrs knobNames x;
  nullAt = paths: x: builtins.foldl' (acc: p: lib.recursiveUpdate acc (lib.setAttrByPath p null)) x paths;

  # true when any leaf under v is set (non-null; lists and strings count).
  anySet =
    v:
    if v == null then
      false
    else if builtins.isAttrs v then
      builtins.any anySet (attrValues v)
    else
      true;

  siteOf = id: builtins.head (lib.splitString "/" id);
  lastSeg = s: if s == null then null else lib.last (lib.splitString "/" s);

  providerOf =
    g:
    let
      site = cfg.sites.${siteOf g.on} or null;
    in
    if site == null then null else site.providers.proxmox or null;

  estateCfg = estate: cfg.estates.${estate} or null;

  # Layered knob values (model names) for one guest.
  effective =
    estate: g:
    let
      isLxc = g.kind == "lxc";
      prov = providerOf g;
      d = if prov == null then null else prov.defaults;
      e = estateCfg estate;
      schemaD = lib.optionalAttrs isLxc {
        unprivileged = true;
        features.nesting = true;
      };
      provD = lib.optionalAttrs (d != null) (
        {
          inherit (d) startOnBoot started;
        }
        // lib.optionalAttrs isLxc {
          console.type = d.console;
          source.osType = d.operatingSystem.type;
        }
      );
      estD = if e == null then { } else nullAt (foreignKnobs g.kind) (knobsOf e.guestDefaults);
      k = builtins.foldl' over { } [
        schemaD
        provD
        estD
        (knobsOf g)
      ];
      placementPool = if e == null || e.placement == null then null else e.placement.pool;
    in
    k
    // {
      pool = if (k.pool or null) != null then k.pool else placementPool;
    };

  # ------------------------------------------------- derived interfaces --
  ipToInt = s: builtins.foldl' (acc: o: acc * 256 + lib.toInt o) 0 (lib.splitString "." s);
  pow2 = n: builtins.foldl' (acc: _: acc * 2) 1 (lib.range 1 n);
  inCidr =
    c: ip:
    let
      parts = lib.splitString "/" c;
      size = pow2 (32 - lib.toInt (builtins.elemAt parts 1));
    in
    (ipToInt ip) / size == (ipToInt (builtins.head parts)) / size;

  siteNetsOf = g: (cfg.sites.${siteOf g.on} or { networks = { }; }).networks;

  ifaceEff =
    g: i: n:
    let
      prov = providerOf g;
      d = if prov == null then null else prov.defaults;
      net = (siteNetsOf g).${n.network} or null;
      gwRouter =
        if net == null || net.gateway == null then
          null
        else
          lib.findFirst (r: r.id == net.gateway) null (attrValues net.routers);
      static = n.address != null && n.address != "dhcp";
    in
    n
    // {
      name =
        if n.name != null then
          n.name
        else if i == 0 && d != null then
          d.nic.name
        else
          "eth${toString i}";
      bridge = if n.bridge != null then n.bridge else if d != null then d.nic.bridge else null;
      prefix =
        if n.prefix != null then
          n.prefix
        else if net != null then
          lib.toInt (lib.last (lib.splitString "/" net.cidr))
        else
          null;
      gateway =
        if n.gateway != null then
          n.gateway
        else if i == 0 && static && gwRouter != null then
          gwRouter.address
        else
          null;
    };
  ifacesOf = g: imap0 (ifaceEff g) g.networkInterfaces;

  ipConfigOf =
    ifs:
    map (
      n:
      if n.address == null then
        { }
      else if n.address == "dhcp" then
        { ipv4.address = "dhcp"; }
      else
        {
          ipv4 = {
            address = "${n.address}/${toString n.prefix}";
            inherit (n) gateway;
          };
        }
    ) ifs;

  # ------------------------------------------------------------ disks --
  sizeGiB =
    s:
    if s == null then
      null
    else if builtins.isInt s then
      s
    else
      let
        n = lib.toInt (lib.removeSuffix "T" (lib.removeSuffix "G" (lib.removeSuffix "M" s)));
      in
      if lib.hasSuffix "T" s then n * 1024 else n;
  sizeStr =
    s:
    if s == null then
      null
    else if builtins.isInt s then
      "${toString s}G"
    else
      s;
  diskDs = k: dk: if dk.datastore != null then dk.datastore else k.defaultDatastore or null;

  # --------------------------------------------------------- references --
  imageById = id: lib.findFirst (i: i.id == id) null (concatMap (s: attrValues s.images) (attrValues cfg.sites));
  pcieById =
    id:
    let
      p = lib.splitString "/" id;
    in
    if length p != 3 then
      null
    else
      let
        site = cfg.sites.${builtins.elemAt p 0} or { nodes = { }; };
        node = site.nodes.${builtins.elemAt p 1} or { pcie = { }; };
      in
      node.pcie.${builtins.elemAt p 2} or null;
  resolveKey =
    ref:
    let
      p = lib.splitString "/" ref;
      ok = length p == 3 && builtins.elemAt p 1 == "ssh";
      pr = cfg.operators.principals.${builtins.elemAt p 0} or null;
      ks = if pr == null then { } else pr.keys or { };
      label = builtins.elemAt p 2;
      k = if !ok then null else (ks.ssh or { }).${label} or (ks.${label} or null);
    in
    if builtins.isAttrs k then k.public or null else if builtins.isString k then k else null;

  # ------------------------------------------------------ provider view --
  # Optional repeatable blocks the baselines never render empty are dropped
  # when empty; mount_point is always rendered (baselines carry []).
  nonEmpty = xs: if xs == [ ] then null else xs;
  prune =
    v:
    if builtins.isAttrs v then
      lib.filterAttrs (_: x: x != null && x != { }) (lib.mapAttrs (_: prune) v)
    else if builtins.isList v then
      map prune v
    else
      v;

  viewOf =
    estate: name: g:
    let
      k = effective estate g;
      ifs = ifacesOf g;
      isLxc = g.kind == "lxc";
      prov = providerOf g;
      d = if prov == null then null else prov.defaults;
      root = lib.findFirst (x: x.role == "root") null g.disks;
      clone = k.source.clone or null;
      tmplImg = if (k.source.template or null) == null then null else imageById k.source.template;
      templateFileId =
        if tmplImg != null then
          tmplImg.volumeId
        else if d != null then
          d.operatingSystem.templateFileId
        else
          null;
      cloneArgs = if clone == null then null else {
        vm_id = clone.vmid;
        datastore_id = lastSeg clone.datastore;
        inherit (clone) full;
        node_name = lastSeg clone.node;
      };
      dns = {
        inherit (k.initialization.dns or { servers = null; domain = null; }) servers domain;
      };
      keys =
        let
          refs = k.initialization.userAccount.keys or null;
        in
        if refs == null then null else map resolveKey refs;
      common = {
        vm_id = g.vmid;
        node_name = lastSeg g.on;
        description = k.description or null;
        tags = k.tags or null;
        pool_id = lastSeg k.pool;
        protection = k.protection or null;
        inherit (k) started;
        startup = {
          order = k.startup.order or null;
          up_delay = k.startup.upDelay or null;
          down_delay = k.startup.downDelay or null;
        };
        clone = cloneArgs;
      };
      cpuCommon = {
        cores = k.cpu.cores or null;
        units = k.cpu.units or null;
        limit = k.cpu.limit or null;
        architecture = k.cpu.architecture or null;
      };
      lxcArgs = common // {
        start_on_boot = k.startOnBoot or null;
        unprivileged = k.unprivileged or null;
        console.type = k.console.type or null;
        cpu = cpuCommon;
        memory = {
          dedicated = k.memory.dedicatedMiB or null;
          swap = k.memory.swapMiB or null;
        };
        features = {
          inherit (k.features or { nesting = null; keyctl = null; fuse = null; mount = null; }) nesting keyctl fuse mount;
        };
        environment_variables = k.environment or null;
        disk =
          if root == null then
            null
          else
            {
              datastore_id = lastSeg (diskDs k root);
              size = sizeGiB root.size;
              inherit (root) quota;
              mount_options = root.mountOptions;
            };
        mount_point = map (m: {
          volume = lastSeg (diskDs k m);
          size = sizeStr m.size;
          inherit (m) path backup quota shared;
          read_only = m.readOnly;
          mount_options = m.mountOptions;
        }) (filter (x: x.role == "mount") g.disks);
        network_interface = map (n: {
          inherit (n) name firewall mtu;
          bridge = lastSeg n.bridge;
          vlan_id = n.vlanId;
          mac_address = n.macAddress;
          rate_limit = n.rateLimit;
        }) ifs;
        initialization = {
          hostname = if (k.initialization.hostname or null) != null then k.initialization.hostname else name;
          inherit dns;
          ip_config = ipConfigOf ifs;
          user_account.keys = keys;
        };
        operating_system =
          if clone != null then
            null
          else
            {
              template_file_id = templateFileId;
              type = k.source.osType or null;
            };
        device_passthrough = nonEmpty (
          map (x: {
            inherit (x) path uid gid mode;
            deny_write = x.denyWrite;
          }) g.devicePassthrough
        );
      };
      vmArgs = common // {
        inherit name;
        on_boot = if (k.startOnBoot or null) == false then false else null;
        cpu = cpuCommon // {
          type = k.cpu.type or null;
        };
        memory = {
          dedicated = k.memory.dedicatedMiB or null;
          floating = k.memory.floatingMiB or null;
        };
        disk = map (x: {
          inherit (x) interface discard iothread ssd cache backup;
          datastore_id = lastSeg (diskDs k x);
          size = sizeGiB x.size;
          file_format = x.fileFormat;
        }) (filter (x: x.role == "root") g.disks ++ filter (x: x.role == "data") g.disks);
        network_device = map (n: {
          inherit (n) firewall mtu model;
          bridge = lastSeg n.bridge;
          vlan_id = n.vlanId;
          mac_address = n.macAddress;
          rate_limit = n.rateLimit;
        }) ifs;
        initialization = {
          datastore_id = lastSeg (k.vm.cloudInit.datastore or null);
          type = k.vm.cloudInit.type or null;
          inherit dns;
          ip_config = ipConfigOf ifs;
          user_account = {
            inherit keys;
            username = k.initialization.userAccount.username or null;
          };
        };
        operating_system.type = k.source.osType or null;
        cdrom =
          let
            c = k.source.cdrom or null;
            img = if c == null then null else imageById c.image;
          in
          if c == null then
            null
          else
            {
              file_id = if img == null then null else img.volumeId;
              inherit (c) interface;
            };
        agent.enabled = k.vm.agent or null;
        bios = k.vm.bios or null;
        machine = k.vm.machine or null;
        serial_device =
          let
            s = k.vm.serialDevices or null;
          in
          if s == null then null else map (x: { device = x; }) s;
        hostpci = nonEmpty (
          imap0 (i: x: {
            device = if x.device != null then x.device else "hostpci${toString i}";
            id =
              let
                p = pcieById x.pcie;
              in
              if p == null then null else p.pci;
            pcie = x.pcieExpress;
            inherit (x) rombar xvga;
          }) g.hostpci
        );
      };
    in
    {
      resource = if isLxc then "proxmox_virtual_environment_container" else "proxmox_virtual_environment_vm";
      args = prune (if isLxc then lxcArgs else vmArgs);
      lifecycle = prune {
        prevent_destroy = if (k.protect or null) == true then true else null;
        ignore_changes = k.ignoreChanges or null;
      };
      companions = prune {
        lxc_extra_conf = g.lxcExtraConf;
      };
    };

  # Flat list of { estate; name; g } for every guest.
  allGuests = concatLists (
    mapAttrsToList (
      estate: gs:
      mapAttrsToList (name: g: {
        inherit estate name g;
      }) gs
    ) cfg.guests
  );

  # ---------------------------------------------------------- assertions --
  check = where: assertion: reason: {
    inherit assertion;
    message = "${where}: ${reason}";
  };

  guestAssertions =
    {
      estate,
      name,
      g,
    }:
    let
      where = "fleet.guests.${estate}.${name}";
      site = siteOf g.on;
      siteNets = siteNetsOf g;
      onOk = elem g.on ids.node;
      prov = providerOf g;
      k = effective estate g;
      isLxc = g.kind == "lxc";
      otherKind = if isLxc then "vm" else "lxc";
      ifs = ifacesOf g;
      hostpciNames = imap0 (i: x: if x.device != null then x.device else "hostpci${toString i}") g.hostpci;
      dupHostpci = lib.unique (filter (x: lib.count (y: y == x) hostpciNames > 1) hostpciNames);
    in
    [
      (h.refAssertion {
        where = "${where}.on";
        kind = "node";
        ids = ids.node;
      } g.on)
    ]
    ++ grantAssertions { inherit estate where g; }
    ++ optional onOk (
      check "${where}.on" (prov != null) "site \"${site}\" has no proxmox provider (fleet.sites.${site}.providers.proxmox)"
    )
    # ---- per-kind fields: only the guest's own definitions are checked ----
    ++ map (
      p:
      check "${where}.${lib.concatStringsSep "." p}" (!anySet (lib.attrByPath p null g))
        "is ${otherKind}-only but kind is \"${g.kind}\""
    ) (foreignKnobs g.kind)
    ++ optionals isLxc [
      (check "${where}.hostpci" (g.hostpci == [ ]) "is vm-only but kind is \"lxc\"")
    ]
    ++ optionals (!isLxc) [
      (check "${where}.lxcExtraConf" (g.lxcExtraConf == null) "is lxc-only but kind is \"vm\"")
      (check "${where}.devicePassthrough" (g.devicePassthrough == [ ]) "is lxc-only but kind is \"vm\"")
    ]
    # ---- provider validators the union types cannot express ----
    ++ [
      (check "${where}.cpu.architecture" (
        k.cpu.architecture == null
        || elem k.cpu.architecture (
          if isLxc then
            [
              "amd64"
              "arm64"
              "armhf"
              "i386"
            ]
          else
            [
              "aarch64"
              "x86_64"
            ]
        )
      ) "cpu.architecture \"${toString k.cpu.architecture}\" is not accepted by the ${g.kind} provider resource (lxc: amd64/arm64/armhf/i386; vm: aarch64/x86_64)")
      (check "${where}.source.osType" (
        k.source.osType == null
        || elem k.source.osType (
          if isLxc then
            [
              "alpine"
              "archlinux"
              "centos"
              "debian"
              "devuan"
              "fedora"
              "gentoo"
              "nixos"
              "opensuse"
              "ubuntu"
              "unmanaged"
            ]
          else
            [
              "l24"
              "l26"
              "other"
              "solaris"
              "w2k"
              "w2k3"
              "w2k8"
              "win7"
              "win8"
              "win10"
              "win11"
              "wvista"
              "wxp"
            ]
        )
      ) "source.osType \"${toString k.source.osType}\" is not accepted by the ${g.kind} provider resource")
      (check "${where}.source.osType" (
        !(isLxc && g.source.clone != null) || g.source.osType == null
      ) "osType is ignored when cloning (operating_system is not sent with clone); remove it")
      (check "${where}.networkInterfaces" (length g.networkInterfaces <= (if isLxc then 10 else 31))
        "too many network interfaces (provider max ${if isLxc then "10" else "31"})"
      )
      (check "${where}.disks" (length g.disks <= (if isLxc then 257 else 31)) "too many disks (provider max ${if isLxc then "256 mount points + root" else "31"})")
      (check "${where}.devicePassthrough" (length g.devicePassthrough <= 128) "too many device_passthrough entries (provider max 128)")
      (check "${where}.hostpci" (length g.hostpci <= 16) "too many hostpci entries (provider max 16)")
      (check "${where}.hostpci" (dupHostpci == [ ]) "duplicate hostpci device slot(s) ${toString dupHostpci}")
      # force the read-only derived values so every checked eval (providerView too) rejects conflicting definitions
      {
        assertion = builtins.seq (builtins.deepSeq g.ipv4 g.effectivePool) true;
        message = "";
      }
    ]
    ++ concatLists (
      imap0 (
        i: n:
        let
          static = n.address != null && n.address != "dhcp";
        in
        [
          (check "${where}.networkInterfaces[${toString i}].gateway" (
            (n.gateway == null && n.prefix == null) || static
          ) "gateway/prefix need a static address")
        ]
      ) g.networkInterfaces
    )
    ++ interfaceAssertions {
      inherit
        where
        g
        site
        siteNets
        onOk
        ifs
        isLxc
        ;
    }
    ++ diskAssertions {
      inherit
        where
        g
        site
        k
        isLxc
        ;
    }
    ++ sourceAssertions {
      inherit
        where
        site
        k
        isLxc
        ;
    }
    ++ poolAssertions {
      inherit where k prov;
    }
    ++ pcieAssertions { inherit where g; }
    ++ keyAssertions { inherit where k; }
    ++ substrateAssertions { inherit estate g where; };

  # ---- network interfaces ----
  interfaceAssertions =
    {
      where,
      g,
      site,
      siteNets,
      onOk,
      ifs,
      isLxc,
    }:
    let
      names = map (n: n.name) ifs;
      dupNames = lib.unique (filter (x: lib.count (y: y == x) names > 1) names);
      staticNets = map (n: n.network) (filter (n: n.address != null && n.address != "dhcp") ifs);
      dupNets = lib.unique (filter (x: lib.count (y: y == x) staticNets > 1) staticNets);
      siteBridges = if cfg.sites ? ${site} then cfg.sites.${site}.bridges else { };
    in
    [
      (check "${where}.networkInterfaces" (dupNames == [ ]) "duplicate interface name(s) ${toString dupNames}")
      (check "${where}.networkInterfaces" (dupNets == [ ])
        "more than one static address on network(s) ${toString dupNets} (ipv4.<network> would be ambiguous)")
    ]
    ++ concatLists (
      imap0 (
        i: n:
        let
          w = "${where}.networkInterfaces[${toString i}]";
          net = siteNets.${n.network} or null;
          br = lib.findFirst (b: b.id == n.bridge) null (attrValues siteBridges);
        in
        optional onOk (
          check "${w}.network" (net != null)
            "network \"${n.network}\" is not a network of site \"${site}\" (known: ${toString (builtins.attrNames siteNets)})"
        )
        ++ optional (!isLxc) (check "${w}.name" (n.name == null || n.name == "eth${toString i}") "vm network_device has no name; leave it unset")
        ++ optional isLxc (check "${w}.model" (n.model == null) "is vm-only but kind is \"lxc\"")
        ++ [
          (check "${w}.bridge" (n.bridge != null) "no bridge (set it, or fleet.sites.${site}.providers.proxmox.defaults.nic.bridge)")
        ]
        ++ optionals (n.bridge != null) (
          [
            (h.refAssertion {
              where = "${w}.bridge";
              kind = "bridge";
              ids = ids.bridge;
            } n.bridge)
          ]
          ++ optional (elem n.bridge ids.bridge) (
            check "${w}.bridge" (br != null) "bridge \"${n.bridge}\" is not a bridge of site \"${site}\""
          )
          ++ optional (br != null && net != null) (
            check "${w}.bridge" (br.network == net.id) "bridge \"${n.bridge}\" carries network ${br.network}, not ${net.id}"
          )
          ++ optional (br != null && br.nodes != [ ]) (
            check "${w}.bridge" (elem g.on br.nodes) "bridge \"${n.bridge}\" is not available on node ${g.on}"
          )
        )
        ++ optional (net != null && n.address != null && n.address != "dhcp") (
          check "${w}.address" (inCidr net.cidr n.address) "address ${n.address} is outside network ${net.id} (${net.cidr})"
        )
      ) ifs
    );

  # ---- disks: datastore of the node's site, on that node; per-kind shape ----
  diskAssertions =
    {
      where,
      g,
      site,
      k,
      isLxc,
    }:
    let
      roots = length (filter (x: x.role == "root") g.disks);
      siteStorage = if cfg.sites ? ${site} then attrValues cfg.sites.${site}.storage else [ ];
      vmIfaces = filter (x: x != null) (map (x: x.interface) g.disks);
      dupIfaces = lib.unique (filter (x: lib.count (y: y == x) vmIfaces > 1) vmIfaces);
      needContent = if isLxc then "rootdir" else "images";
    in
    [
      (check "${where}.disks" (roots == 1) "${if isLxc then "an lxc" else "a vm"} guest needs exactly one disk with role \"root\" (found ${toString roots})")
    ]
    ++ optional (!isLxc) (check "${where}.disks" (dupIfaces == [ ]) "duplicate disk interface(s) ${toString dupIfaces}")
    ++ concatLists (
      imap0 (
        i: x:
        let
          w = "${where}.disks[${toString i}]";
          ds = diskDs k x;
          st = lib.findFirst (s: s.id == ds) null siteStorage;
        in
        optional isLxc (check "${w}.role" (x.role != "data") "role \"data\" is vm-only; an lxc uses \"mount\"")
        ++ optional (!isLxc) (check "${w}.role" (x.role != "mount") "role \"mount\" is lxc-only; a vm uses \"data\"")
        ++ optional (isLxc && x.role == "mount") (check "${w}.path" (x.path != null) "a mount disk needs a path")
        ++ optional (isLxc && x.role == "root") (check "${w}.path" (x.path == null) "the root disk has no path")
        ++ optionals (isLxc && x.role == "root") (
          map (f: check "${w}.${f}" (x.${f} == null) "not an argument of the container root disk (mount disks only)") [
            "readOnly"
            "shared"
            "backup"
          ]
        )
        ++ optional (!isLxc) (check "${w}.interface" (x.interface != null) "a vm disk needs an interface (e.g. scsi0, virtio0)")
        ++ optional (!isLxc && builtins.isString x.size) (
          check "${w}.size" (!lib.hasSuffix "M" (toString x.size)) "vm disk sizes are whole GiB"
        )
        ++ optional (isLxc && x.role == "root" && builtins.isString x.size) (
          check "${w}.size" (!lib.hasSuffix "M" (toString x.size)) "the lxc root disk size is whole GiB"
        )
        ++ map (f: check "${w}.${f}" (x.${f} == null) "is ${otherKindOf isLxc}-only but kind is \"${g.kind}\"") (
          if isLxc then vmOnlyDiskFields else lxcOnlyDiskFields
        )
        ++ [
          (check "${w}.datastore" (ds != null) "no datastore (set it, or guestDefaults.defaultDatastore)")
        ]
        ++ optionals (ds != null) (
          [
            (h.refAssertion {
              where = "${w}.datastore";
              kind = "storage";
              ids = ids.storage;
            } ds)
          ]
          ++ optional (elem ds ids.storage) (
            check "${w}.datastore" (st != null) "storage \"${ds}\" is not a storage of site \"${site}\" (the site of node ${g.on})"
          )
          ++ optional (st != null && st.nodes != [ ]) (
            check "${w}.datastore" (elem g.on st.nodes) "storage \"${ds}\" is not available on node ${g.on} (its nodes: ${toString st.nodes})"
          )
          ++ optional (st != null) (
            check "${w}.datastore" (elem needContent st.content) "storage \"${ds}\" does not hold content \"${needContent}\""
          )
        )
      ) g.disks
    );
  otherKindOf = isLxc: if isLxc then "vm" else "lxc";

  # ---- source: template image / clone / cdrom ----
  sourceAssertions =
    {
      where,
      site,
      k,
      isLxc,
    }:
    let
      w = "${where}.source";
      clone = k.source.clone or null;
      tmpl = k.source.template or null;
      cdrom = k.source.cdrom or null;
      imgCheck =
        field: id: content:
        let
          img = imageById id;
        in
        [
          (h.refAssertion {
            where = "${w}.${field}";
            kind = "image";
            ids = ids.image;
          } id)
        ]
        ++ optional (img != null) (check "${w}.${field}" (siteOf img.id == site) "image \"${id}\" is not an image of site \"${site}\"")
        ++ optional (img != null) (
          check "${w}.${field}" (img.contentType == content) "image \"${id}\" has content type \"${img.contentType}\", expected \"${content}\""
        );
    in
    optional (isLxc && clone != null) (check w (tmpl == null) "source.clone and source.template are exclusive")
    ++ optionals (isLxc && tmpl != null) (imgCheck "template" tmpl "vztmpl")
    ++ optionals (!isLxc && cdrom != null) (imgCheck "cdrom.image" cdrom.image "iso")
    ++ optionals (clone != null) (
      h.optionalRefAssertions {
        where = "${w}.clone.datastore";
        kind = "storage";
        ids = ids.storage;
      } clone.datastore
      ++ h.optionalRefAssertions {
        where = "${w}.clone.node";
        kind = "node";
        ids = ids.node;
      } clone.node
    )
    ++ h.optionalRefAssertions {
      where = "${where}.vm.cloudInit.datastore";
      kind = "storage";
      ids = ids.storage;
    } (k.vm.cloudInit.datastore or null);

  # ---- pool: an existing pool of the guest's provider ----
  poolAssertions =
    {
      where,
      k,
      prov,
    }:
    let
      pool = lib.findFirst (p: p.id == k.pool) null (
        concatMap (e: concatMap attrValues (attrValues e.pools)) (attrValues cfg.estates)
      );
    in
    h.optionalRefAssertions {
      where = "${where}.pool";
      kind = "pool";
      ids = ids.pool;
    } k.pool
    ++ optional (pool != null && prov != null) (
      check "${where}.pool" (pool.provider == prov.id) "pool \"${k.pool}\" belongs to provider ${pool.provider}, not ${prov.id} (the guest's site provider)"
    );

  # ---- pcie references (hostpci, devicePassthrough provenance) ----
  pcieAssertions =
    { where, g }:
    let
      one =
        w: id: needPci:
        let
          p = pcieById id;
        in
        [
          (h.refAssertion {
            where = w;
            kind = "pcie";
            ids = ids.pcie;
          } id)
        ]
        ++ optional (p != null) (
          check w (lib.hasPrefix "${g.on}/" id) "pcie device \"${id}\" is not on the guest's node ${g.on}"
        )
        ++ optional (p != null && needPci) (check w (p.pci != null) "pcie device \"${id}\" has no pci address");
    in
    concatLists (imap0 (i: x: one "${where}.hostpci[${toString i}].pcie" x.pcie true) g.hostpci)
    ++ concatLists (
      imap0 (
        i: x: lib.optionals (x.pcie != null) (one "${where}.devicePassthrough[${toString i}].pcie" x.pcie false)
      ) g.devicePassthrough
    );

  # ---- initialization.userAccount.keys: principal ssh key references ----
  keyAssertions =
    { where, k }:
    map (
      ref:
      check "${where}.initialization.userAccount.keys" (resolveKey ref != null)
        "key reference \"${ref}\" does not resolve to a principal ssh public key (form <principal>/ssh/<label>)"
    ) (if (k.initialization.userAccount.keys or null) == null then [ ] else k.initialization.userAccount.keys);

  # ---- substrate tags vs estate substrate lists ----
  roleMembers =
    estate: role:
    let
      sub = if cfg.estates ? ${estate} then cfg.estates.${estate}.substrate else null;
      v =
        if sub == null then
          [ ]
        else if role == "secretsEngine" then
          sub.secretsEngine.guests or [ ]
        else
          sub.${role} or [ ];
    in
    if v == null then [ ] else v;

  substrateAssertions =
    {
      estate,
      g,
      where,
    }:
    map (role: {
      assertion = elem g.id (roleMembers estate role);
      message = "${where}.substrate: tagged \"${role}\" but fleet.estates.${estate}.substrate does not list ${g.id} for that role";
    }) g.substrate
    ++ map (role: {
      assertion = elem role g.substrate;
      message = "fleet.estates.${estate}.substrate lists ${g.id} for \"${role}\" but the guest does not carry that substrate tag";
    }) (filter (role: elem g.id (roleMembers estate role)) substrateRoles);

  # ---- the grant: a guest stays inside its pool's vmid and host ranges ----
  # A pool is the estate's own (<estate>/<platform>/<name>); an empty range
  # list is no limit. The lab sets the ranges, so this is what stops a tenant
  # from taking ids or addresses it was not given.
  inRanges = ranges: n: ranges == [ ] || lib.any (r: r.from <= n && n <= r.to) ranges;
  showRanges = ranges: lib.concatStringsSep ", " (map (r: "${toString r.from}-${toString r.to}") ranges);
  grantAssertions =
    {
      estate,
      where,
      g,
    }:
    let
      parts = lib.splitString "/" (if g.effectivePool == null then "" else g.effectivePool);
      own = builtins.length parts == 3 && builtins.head parts == estate;
      pool =
        if own then
          cfg.estates.${estate}.pools.${builtins.elemAt parts 1}.${builtins.elemAt parts 2} or null
        else
          null;
    in
    optional (g.effectivePool != null) (
      check "${where}.pool" own "pool \"${g.effectivePool}\" is not a pool of estate \"${estate}\""
    )
    ++ optionals (pool != null) (
      [
        (check "${where}.vmid" (inRanges pool.vmid g.vmid)
          "vmid ${toString g.vmid} is outside the ranges granted to pool ${g.effectivePool} (${showRanges pool.vmid})"
        )
      ]
      ++ lib.concatLists (
        lib.mapAttrsToList (
          net: p:
          optional (g.ipv4 ? ${net}) (
            check "${where}.networkInterfaces" (inRanges p.hosts (lib.toInt (lib.last (lib.splitString "." g.ipv4.${net}))))
              "address ${g.ipv4.${net}} on \"${net}\" is outside the host ranges granted to pool ${g.effectivePool} (${showRanges p.hosts})"
          )
        ) pool.networks
      )
    );

  estateKeyAssertions = map (
    estate:
    h.refAssertion {
      where = "fleet.guests.${estate}";
      kind = "estate";
      ids = ids.estate;
    } estate
  ) (builtins.attrNames cfg.guests);

  # ---- estate guestDefaults: references are checked even if every guest overrides them ----
  defaultsAssertions = concatLists (
    mapAttrsToList (
      ename: e:
      let
        w = "fleet.estates.${ename}.guestDefaults";
        gd = e.guestDefaults;
      in
      h.optionalRefAssertions {
        where = "${w}.pool";
        kind = "pool";
        ids = ids.pool;
      } gd.pool
      ++ h.optionalRefAssertions {
        where = "${w}.defaultDatastore";
        kind = "storage";
        ids = ids.storage;
      } gd.defaultDatastore
      ++ h.optionalRefAssertions {
        where = "${w}.source.template";
        kind = "image";
        ids = ids.image;
      } gd.source.template
    ) cfg.estates
  );

  # ---- vmid collisions (per cluster = site of the node) ----
  vmidGroups = lib.groupBy (x: "${siteOf x.g.on}#${toString x.g.vmid}") allGuests;
  vmidCollisions = lib.mapAttrsToList (_: xs: {
    cluster = siteOf (builtins.head xs).g.on;
    inherit ((builtins.head xs).g) vmid;
    claimants = lib.sort lib.lessThan (map (x: x.g.id) xs);
  }) (lib.filterAttrs (_: xs: length xs > 1) vmidGroups);

  sameClaim =
    a: b:
    a.cluster == b.cluster && a.vmid == b.vmid && lib.sort lib.lessThan a.claimants == b.claimants;
  allowed = c: builtins.any (a: sameClaim a c) cfg.allow.vmidCollisions;

  vmidAssertions =
    map (c: {
      assertion = allowed c;
      message = "vmid ${toString c.vmid} is used more than once in cluster \"${c.cluster}\": ${toString c.claimants} (allowlist it in fleet.allow.vmidCollisions with a reason if intended)";
    }) vmidCollisions
    ++ map (a: {
      assertion = builtins.any (c: sameClaim a c) vmidCollisions;
      message = "fleet.allow.vmidCollisions: stale entry for vmid ${toString a.vmid} in \"${a.cluster}\" (${toString a.claimants}) no longer matches a collision; remove it";
    }) cfg.allow.vmidCollisions;

  # ---- IP claims: guests (derived ipv4), nodes, routers ----
  ipClaims =
    concatMap (
      { g, ... }:
      map (ip: {
        inherit ip;
        inherit (g) id;
      }) (attrValues g.ipv4)
    ) allGuests
    ++ concatMap (
      site:
      map (n: {
        ip = n.address;
        inherit (n) id;
      }) (attrValues site.nodes)
      ++ concatMap (
        net:
        concatMap (
          r:
          optional ((r.address or null) != null) {
            ip = r.address;
            inherit (r) id;
          }
        ) (attrValues net.routers)
      ) (attrValues site.networks)
    ) (attrValues cfg.sites);
  ipGroups = lib.groupBy (c: c.ip) ipClaims;
  ipCollisions = lib.mapAttrsToList (ip: cs: {
    inherit ip;
    claimants = lib.sort lib.lessThan (map (c: c.id) cs);
  }) (lib.filterAttrs (_: cs: length cs > 1) ipGroups);

  # ---- every modelled guest option path (for tests/guest_fidelity.py) ----
  # "*" marks a list element. Submodule internals (_module) are skipped.
  visible = lib.filterAttrs (n: _: !lib.hasPrefix "_" n);
  isListType = t: t.name == "listOf" || (t.name == "nullOr" && t.nestedTypes.elemType.name == "listOf");
  optionPaths =
    path: x:
    if lib.isOption x then
      let
        sub = visible (x.type.getSubOptions [ ]);
        star = optional (isListType x.type) "*";
      in
      if sub == { } then
        [ (lib.concatStringsSep "." path) ]
      else
        concatLists (mapAttrsToList (n: v: optionPaths (path ++ star ++ [ n ]) v) sub)
    else
      concatLists (mapAttrsToList (n: v: optionPaths (path ++ [ n ]) v) (visible x));
in
{
  options.fleet = {
    guests = mkOption {
      type = types.attrsOf estateGuestsType;
      default = { };
      description = "Guests keyed by estate name, then guest name.";
    };

    # Extends the estate submodule declared in ./estates.nix.
    estates = mkOption {
      type = types.attrsOf (
        types.submodule {
          options.guestDefaults = mkOption {
            type = types.submodule { options = knobOptions; };
            default = { };
            description = "Estate layer of the guest knobs (between the site provider defaults and the guest). null = silent; knobs that do not exist for a guest's kind are skipped for that guest.";
          };
        }
      );
    };

    allow.vmidCollisions = mkOption {
      type = types.listOf (
        types.submodule {
          options = {
            cluster = mkOption {
              type = types.str;
              description = "Site id whose Proxmox cluster holds both guests.";
            };
            vmid = mkOption { type = types.int; };
            claimants = mkOption {
              type = types.listOf types.str;
              description = "Guest ids sharing the vmid (any order).";
            };
            reason = mkOption { type = types.strMatching ".*[^[:space:]].*"; };
          };
        }
      );
      default = [ ];
      description = "Accepted vmid collisions. Each must match a real collision exactly.";
    };

    report = {
      providerView = mkOption {
        type = types.attrsOf (types.attrsOf jsonValue);
        readOnly = true;
        default = lib.mapAttrs (estate: gs: lib.mapAttrs (viewOf estate) gs) cfg.guests;
        description = ''
          Derived, read-only: per guest, the effective provider view after
          layering, in bpg/proxmox 0.115.0 argument names:
            { resource; args; lifecycle; companions; }
          args: single blocks are objects (cpu, memory, disk for lxc, ...),
          repeatable blocks are lists (network_interface, mount_point,
          ip_config, vm disk, hostpci, ...); null leaves and empty blocks are
          dropped. lifecycle: prevent_destroy / ignore_changes (Terraform
          meta-arguments). companions.lxc_extra_conf: raw lxc.conf lines.
        '';
      };
      guestOptionPaths = mkOption {
        type = types.listOf types.str;
        readOnly = true;
        default = lib.sort lib.lessThan (optionPaths [ ] ((guestType "estate").getSubOptions [ ]));
        description = "Derived, read-only: every guest option path (\"*\" = list element). docs/guest-provider-map.md must map each one.";
      };
    };
  };

  config = {
    fleet.report = {
      inherit ipCollisions;
      vmidCollisions = map (
        c: c // { inherit (lib.findFirst (a: sameClaim a c) null cfg.allow.vmidCollisions) reason; }
      ) (filter allowed vmidCollisions);
    };

    assertions =
      estateKeyAssertions ++ defaultsAssertions ++ concatMap guestAssertions allGuests ++ vmidAssertions;
  };
}
