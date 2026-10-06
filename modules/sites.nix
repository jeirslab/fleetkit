# fleet.sites: physical sites and everything inside them (networks, routers,
# bridges, nodes, pcie devices, providers, storage, images).
#
# Fully typed: no freeform at any level, so a typo'd option name fails with
# "does not exist". Every reference leaf is validated in config.assertions.
{ lib, config, ... }:
let
  inherit (lib) types mkOption;
  h = import ./lib.nix { inherit lib; };
  inherit (config.fleet.report) ids;

  octet = "(25[0-5]|2[0-4][0-9]|1[0-9][0-9]|[1-9]?[0-9])";
  ipv4Re = "(${octet}\\.){3}${octet}";
  ipv4 = types.strMatching ipv4Re;
  cidr = types.strMatching "${ipv4Re}/(3[0-2]|[12]?[0-9])";
  pciAddr = types.strMatching "[0-9a-fA-F]{4}:[0-9a-fA-F]{2}:[0-9a-fA-F]{2}(\\.[0-7])?";
  # Plain str: a strMatching type error would echo a pasted secret; the
  # secretRefAssertions in this file validate the shape and withhold the value.
  # (sops-nix is a NixOS-side secret mechanism and does not apply to refs here:
  # a ref names a key in a declared sops file, resolved by the fleet CLI.)
  sopsRef = types.str;

  contentKinds = [
    "rootdir"
    "images"
    "vztmpl"
    "iso"
    "backup"
    "snippets"
    "import"
  ];

  req = type: mkOption { inherit type; };
  opt =
    type: default:
    mkOption { inherit type default; };
  nullable = type: opt (types.nullOr type) null;

  # IPv4 arithmetic for the in-CIDR checks.
  ipToInt =
    s:
    builtins.foldl' (acc: o: acc * 256 + lib.toInt o) 0 (lib.splitString "." s);
  pow2 = n: builtins.foldl' (acc: _: acc * 2) 1 (lib.range 1 n);
  inCidr =
    c: ip:
    let
      parts = lib.splitString "/" c;
      size = pow2 (32 - lib.toInt (builtins.elemAt parts 1));
    in
    (ipToInt ip) / size == (ipToInt (builtins.head parts)) / size;

  routerType =
    parent:
    types.submodule (
      { name, ... }:
      {
        options = {
          id = h.mkId (h.qualify parent name);
          external = opt types.bool false;
          managed = opt types.bool false;
          platform = nullable types.str;
          address = nullable ipv4;
          provides = opt (types.listOf (types.enum [
            "gateway"
            "dns"
            "dhcp"
          ])) [ ];
          priority = nullable types.int;
          node = nullable types.str;
          status = opt (types.enum [
            "active"
            "planned"
          ]) "active";
        };
      }
    );

  networkType =
    site:
    types.submodule (
      { name, ... }:
      let
        id = h.qualify site name;
      in
      {
        options = {
          id = h.mkId id;
          cidr = req cidr;
          gateway = nullable types.str;
          dns = opt (types.listOf ipv4) [ ];
          domains = opt (types.listOf types.str) [ ];
          searchDomains = opt (types.listOf types.str) [ ];
          tailnet = nullable types.str;
          routers = opt (types.attrsOf (routerType id)) { };
        };
      }
    );

  bridgeType =
    site:
    types.submodule (
      { name, ... }:
      {
        options = {
          id = h.mkId (h.qualify site name);
          network = req types.str;
          nodes = opt (types.listOf types.str) [ ];
        };
      }
    );

  pcieType =
    node:
    types.submodule (
      { name, ... }:
      {
        options = {
          id = h.mkId (h.qualify node name);
          kind = req (types.enum [ "gpu" ]);
          name = req types.str;
          pci = nullable pciAddr;
        };
      }
    );

  nodeType =
    site:
    types.submodule (
      { name, ... }:
      let
        id = h.qualify site name;
      in
      {
        options = {
          id = h.mkId id;
          osType = req (types.enum [
            "debian-pve"
            "nixos"
          ]);
          address = req ipv4;
          hostKeys.ed25519 = nullable (types.strMatching "ssh-ed25519 AAAA[A-Za-z0-9+/]+={0,2}");
          pcie = opt (types.attrsOf (pcieType id)) { };
        };
      }
    );

  providerType =
    site:
    types.submodule (
      { name, ... }:
      {
        options = {
          id = h.mkId (h.qualify site name);
          api = req types.str;
          tokenRef = req sopsRef;
          insecureTls = req types.bool;
          ssh = req (
            types.submodule {
              options = {
                agent = req types.bool;
                username = req types.str;
              };
            }
          );
          ids = req (
            types.submodule {
              options.vmid.scope = req (
                types.enum [
                  "cluster"
                  "node"
                ]
              );
            }
          );
          nodes = opt (types.attrsOf (
            types.submodule {
              options.clusterInitiator = opt types.bool false;
            }
          )) { };
          defaults = req (
            types.submodule {
              options = {
                console = req types.str;
                nic = req (
                  types.submodule {
                    options = {
                      name = req types.str;
                      bridge = req types.str;
                    };
                  }
                );
                operatingSystem = req (
                  types.submodule {
                    options = {
                      type = req types.str;
                      templateFileId = req types.str;
                    };
                  }
                );
                startOnBoot = req types.bool;
                started = req types.bool;
              };
            }
          );
        };
      }
    );

  storageType =
    site:
    types.submodule (
      { name, ... }:
      {
        options = {
          id = h.mkId (h.qualify site name);
          type = req (types.enum [
            "lvmthin"
            "dir"
            "zfspool"
            "nfs"
          ]);
          vgname = nullable types.str;
          thinpool = nullable types.str;
          path = nullable types.str;
          pool = nullable types.str;
          mountpoint = nullable types.str;
          server = nullable ipv4;
          export = nullable types.str;
          options = nullable types.str;
          content = opt (types.listOf (types.enum contentKinds)) [ ];
          capacityTB = nullable types.number;
          pruneBackups = nullable types.str;
          shared = nullable types.bool;
          sparse = nullable types.bool;
          nodes = opt (types.listOf types.str) [ ];
          estates = opt (types.listOf types.str) [ ];
        };
      }
    );

  imageType =
    site:
    types.submodule (
      { name, config, ... }:
      {
        options = {
          id = h.mkId (h.qualify site name);
          datastore = mkOption {
            type = types.str;
            description = "Storage id (site/storage) holding the image.";
          };
          contentType = req (types.enum contentKinds);
          fileName = req types.str;
          node = nullable types.str;
          build = req (
            types.submodule {
              options.method = req (types.enum [ "nix" ]);
            }
          );
          volumeId = mkOption {
            type = types.str;
            readOnly = true;
            default = "${lib.last (lib.splitString "/" config.datastore)}:${config.contentType}/${config.fileName}";
            description = "Proxmox volume id, derived: <storage name>:<contentType>/<fileName>.";
          };
        };
      }
    );

  siteType = types.submodule (
    { name, ... }:
    {
      options = {
        id = h.mkId name;
        region = req types.str;
        networks = opt (types.attrsOf (networkType name)) { };
        bridges = opt (types.attrsOf (bridgeType name)) { };
        nodes = opt (types.attrsOf (nodeType name)) { };
        providers = opt (types.attrsOf (providerType name)) { };
        storage = opt (types.attrsOf (storageType name)) { };
        images = opt (types.attrsOf (imageType name)) { };
      };
    }
  );

  inherit (h) idsOf;
  check = where: assertion: reason: {
    inherit assertion;
    message = "${where}: ${reason}";
  };

  siteAssertions =
    sname: site:
    let
      p = "fleet.sites.${sname}";
      siteNodes = idsOf site.nodes;
      siteBridges = idsOf site.bridges;
      siteNetworks = idsOf site.networks;
      siteStorage = idsOf site.storage;
      siteImages = lib.mapAttrsToList (_: i: i.volumeId) site.images;
    
      cidrs = lib.mapAttrsToList (_: n: n.cidr) site.networks;
      inSomeNetwork = ip: lib.any (c: inCidr c ip) cidrs;

      networkAssertions = lib.concatLists (
        lib.mapAttrsToList (
          nname: net:
          let
            np = "${p}.networks.${nname}";
          in
          lib.optional (net.gateway != null) (
            h.refAssertion {
              where = "${np}.gateway";
              kind = "router";
              ids = idsOf net.routers;
            } net.gateway
          )
          ++ map (h.refAssertion {
            where = "${np}.domains";
            kind = "zone";
            ids = ids.zone;
          }) net.domains
          ++ map (h.refAssertion {
            where = "${np}.searchDomains";
            kind = "zone";
            ids = ids.zone;
          }) net.searchDomains
          ++ h.optionalRefAssertions {
            where = "${np}.tailnet";
            kind = "tailnet";
            ids = ids.tailnet;
          } net.tailnet
          ++ lib.concatLists (
            lib.mapAttrsToList (
              rname: r:
              h.optionalRefAssertions {
                where = "${np}.routers.${rname}.node";
                kind = "node";
                ids = siteNodes;
              } r.node
              ++ lib.optional (r.address != null) (
                check "${np}.routers.${rname}.address" (inCidr net.cidr r.address)
                  "address ${r.address} is outside the network cidr ${net.cidr}"
              )
            ) net.routers
          )
        ) site.networks
      );

      bridgeAssertions = lib.concatLists (
        lib.mapAttrsToList (
          bname: b:
          [
            (h.refAssertion {
              where = "${p}.bridges.${bname}.network";
              kind = "network";
              ids = siteNetworks;
            } b.network)
          ]
          ++ h.refAssertions {
            where = "${p}.bridges.${bname}.nodes";
            kind = "node";
            ids = siteNodes;
          } b.nodes
        ) site.bridges
      );

      nodeAssertions = lib.mapAttrsToList (
        nname: n:
        check "${p}.nodes.${nname}.address" (inSomeNetwork n.address)
          "address ${n.address} is outside every network cidr of site ${sname}"
      ) site.nodes;

      providerAssertions = lib.concatLists (
        lib.mapAttrsToList (
          kname: prov:
          let
            pp = "${p}.providers.${kname}";
            initiators = lib.attrNames (lib.filterAttrs (_: n: n.clusterInitiator) prov.nodes);
          in
          [
            (check pp (builtins.elem kname [ "proxmox" ])
              "provider kind \"${kname}\" is not supported (supported: proxmox)")
            (check "${pp}.nodes" (builtins.length initiators <= 1)
              "at most one node may set clusterInitiator, found: ${lib.concatStringsSep ", " initiators}")
            (h.refAssertion {
              where = "${pp}.defaults.nic.bridge";
              kind = "bridge";
              ids = siteBridges;
            } prov.defaults.nic.bridge)
            (check "${pp}.defaults.operatingSystem.templateFileId"
              (builtins.elem prov.defaults.operatingSystem.templateFileId siteImages)
              "template file id \"${prov.defaults.operatingSystem.templateFileId}\" is not the volumeId of any image of site ${sname} (known: ${lib.concatStringsSep ", " siteImages})")
          ]
          ++ map (
            nodeName:
            h.refAssertion {
              where = "${pp}.nodes.${nodeName}";
              kind = "node";
              ids = siteNodes;
            } (h.qualify sname nodeName)
          ) (lib.attrNames prov.nodes)
          # sites have no owning estate: membership in declared sops keys only
          ++ h.secretRefAssertions {
            where = "${pp}.tokenRef";
            ids = ids.secretRef;
          } prov.tokenRef
        ) site.providers
      );

      storageAssertions = lib.concatLists (
        lib.mapAttrsToList (
          stname: st:
          let
            sp = "${p}.storage.${stname}";
            need =
              fields:
              map (
                f:
                check "${sp}.${f}" (st.${f} != null) "required for storage type \"${st.type}\""
              ) fields;
          in
          (
            if st.type == "lvmthin" then
              need [
                "vgname"
                "thinpool"
              ]
            else if st.type == "dir" then
              need [ "path" ]
            else if st.type == "zfspool" then
              need [ "pool" ]
            else
              need [
                "server"
                "export"
                "path"
              ]
          )
          ++ h.refAssertions {
            where = "${sp}.nodes";
            kind = "node";
            ids = siteNodes;
          } st.nodes
          ++ h.refAssertions {
            where = "${sp}.estates";
            kind = "estate";
            ids = ids.estate;
          } st.estates
        ) site.storage
      );

      imageAssertions = lib.concatLists (
        lib.mapAttrsToList (
          iname: img:
          let
            ip = "${p}.images.${iname}";
            ds = lib.findFirst (s: s.id == img.datastore) null (lib.attrValues site.storage);
          in
          [
            (h.refAssertion {
              where = "${ip}.datastore";
              kind = "storage";
              ids = ids.storage;
            } img.datastore)
            (check "${ip}.datastore" (
              !(builtins.elem img.datastore ids.storage) || builtins.elem img.datastore siteStorage
            ) "storage \"${img.datastore}\" is not a storage of site ${sname} (site storage ids: ${lib.concatStringsSep ", " siteStorage})")
            (check "${ip}.contentType" (
              ds == null || builtins.elem img.contentType ds.content
            ) "storage \"${img.datastore}\" does not list content type \"${img.contentType}\"")
          ]
          ++ h.optionalRefAssertions {
            where = "${ip}.node";
            kind = "node";
            ids = siteNodes;
          } img.node
        ) site.images
      );
    in
    networkAssertions
    ++ bridgeAssertions
    ++ nodeAssertions
    ++ providerAssertions
    ++ storageAssertions
    ++ imageAssertions;
in
{
  options.fleet.sites = mkOption {
    type = types.attrsOf siteType;
    default = { };
  };

  config.assertions = lib.concatLists (lib.mapAttrsToList siteAssertions config.fleet.sites);
}
