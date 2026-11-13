# Minimal fleet data for tests/terraform.sh: one site, one node and the
# estate "mini" with a pool, two managed guests (lxc, vm), one managed lxc
# guest with an lxc_extra_conf companion and one adopted guest, and its own
# placement.tokenRef. Two more estates share the site's provider: "tenant"
# (placement without a tokenRef, a pool and a guest) and "bare" (no placement,
# one guest). "gaps" (placement without a tokenRef) uses every option added for
# adopting existing guests: a pool with a comment, a container with an idmap,
# no console block and in no pool, a container with lxc_extra_conf (description
# ignored) and a second interface on a network its VLAN-aware bridge tags, a VM with scsi hardware, an EFI disk, a cloud-init drive slot and a
# clone source (clone ignored). "quiet" (no placement) sets
# adoption.unrecorded = "ignore" in its guestDefaults: a container and a VM
# take it, a container that already ignores some arguments takes it without
# duplicates, and a container and a VM set it back to "apply". "tmpl" (no
# placement) has a VM template whose root disk is imported from an image the
# deploy uploads (s1/vyos, a source), and a VM cloned from it by reference.
# "addon" (no placement) switches the Ansible add-on on: a playbook directory
# (playbooks/vyos, its entry site.yml) against a managed VM, with collections,
# groups, extra variables and a secret as a file path, its SSH user from
# colmena.targetUser; and a playbook file against an adopted container, with
# its own user. No other estate enables an add-on.
# "split" has guests on two sites and must not render; "offsite" has a placement on s1 and a guest on s2, and must not render. Generic
# names only; nothing here is a real address.
_: {
  fleet = {
    sites.s1 = {
      region = "test";
      networks.lan.cidr = "192.0.2.0/24";
      # Carried tagged on vmbr0, which is VLAN-aware (gaps.conf's second
      # interface; tests/terraform.sh checks its vlan_id).
      networks.tagged.cidr = "203.0.113.0/24";
      bridges.vmbr0 = {
        network = "s1/lan";
        vlans.tagged = 7;
      };
      nodes.n1 = {
        osType = "debian-pve";
        address = "192.0.2.10";
      };
      storage.local = {
        type = "dir";
        path = "/var/lib/vz";
        content = [
          "rootdir"
          "images"
          "vztmpl"
          "import"
        ];
      };
      images.tpl = {
        datastore = "s1/local";
        contentType = "vztmpl";
        fileName = "tpl.tar.zst";
        build.method = "nix";
      };
      # A disk image the deploy uploads (a store path, as a built image's
      # would be; nothing here builds or reads it).
      images.vyos = {
        datastore = "s1/local";
        contentType = "import";
        fileName = "vyos-1.qcow2";
        node = "s1/n1";
        source = "/nix/store/00000000000000000000000000000000-vyos.qcow2";
        build.method = "nix";
      };
      providers.proxmox = {
        api = "https://192.0.2.10:8006/";
        # The cluster's credential, in estate mini's secrets. Deliberately not
        # mini's placement.tokenRef: mini must render its own override, the
        # other estates this one.
        tokenRef = "sops:mini/tf#site-token";
        insecureTls = true;
        ssh = {
          agent = true;
          username = "root";
        };
        ids.vmid.scope = "cluster";
        defaults = {
          console = "console";
          nic = {
            name = "eth0";
            bridge = "s1/vmbr0";
          };
          operatingSystem = {
            type = "nixos";
            templateFileId = "local:vztmpl/tpl.tar.zst";
          };
          startOnBoot = true;
          started = true;
        };
      };
    };

    sites.s2 = {
      region = "test";
      networks.lan.cidr = "198.51.100.0/24";
      bridges.vmbr0.network = "s2/lan";
      nodes.n1 = {
        osType = "debian-pve";
        address = "198.51.100.10";
      };
      storage.local = {
        type = "dir";
        path = "/var/lib/vz";
        content = [
          "rootdir"
          "images"
          "vztmpl"
        ];
      };
      images.tpl = {
        datastore = "s2/local";
        contentType = "vztmpl";
        fileName = "tpl.tar.zst";
        build.method = "nix";
      };
      providers.proxmox = {
        api = "https://198.51.100.10:8006/";
        tokenRef = "sops:mini/tf#site-token";
        insecureTls = true;
        ssh = {
          agent = true;
          username = "root";
        };
        ids.vmid.scope = "cluster";
        defaults = {
          console = "console";
          nic = {
            name = "eth0";
            bridge = "s2/vmbr0";
          };
          operatingSystem = {
            type = "nixos";
            templateFileId = "local:vztmpl/tpl.tar.zst";
          };
          startOnBoot = true;
          started = true;
        };
      };
    };

    estates = {
      mini = {
        owner = "Mini";
        secrets = {
          backend = "sops";
          files.tf = {
            path = "secrets/tf.json";
            keys = [
              "integrations/proxmox/main/api_token"
              "site-token"
            ];
          };
        };
        placement = {
          provider = "s1/proxmox";
          pool = "mini/proxmox/main";
          tokenRef = "sops:mini/tf#integrations/proxmox/main/api_token";
        };
        pools.proxmox.main.provider = "s1/proxmox";
        guestDefaults.defaultDatastore = "s1/local";
      };

      tenant = {
        owner = "Tenant";
        placement = {
          provider = "s1/proxmox";
          pool = "tenant/proxmox/tpool";
        };
        pools.proxmox.tpool.provider = "s1/proxmox";
        guestDefaults.defaultDatastore = "s1/local";
      };

      bare = {
        owner = "Bare";
        guestDefaults.defaultDatastore = "s1/local";
      };

      offsite = {
        owner = "Offsite";
        placement = {
          provider = "s1/proxmox";
          pool = "offsite/proxmox/near";
        };
        pools.proxmox.near.provider = "s1/proxmox";
        pools.proxmox.far.provider = "s2/proxmox";
        guestDefaults.defaultDatastore = "s1/local";
      };

      split = {
        owner = "Split";
        guestDefaults.defaultDatastore = "s1/local";
      };

      gaps = {
        owner = "Gaps";
        placement = {
          provider = "s1/proxmox";
          pool = "gaps/proxmox/gpool";
        };
        pools.proxmox.gpool = {
          provider = "s1/proxmox";
          comment = "guests of the gaps estate";
          vmid = [
            {
              from = 9500;
              to = 9599;
            }
          ];
        };
        guestDefaults.defaultDatastore = "s1/local";
      };

      tmpl = {
        owner = "Tmpl";
        guestDefaults.defaultDatastore = "s1/local";
      };

      addon = {
        owner = "Addon";
        guestDefaults.defaultDatastore = "s1/local";
        colmena.targetUser = "vyos";
        addons.ansible = {
          enable = true;
          playbooks = {
            vyos = {
              guest = "router";
              playbook = ./playbooks/vyos;
              entry = "site.yml";
              collections = [
                "vyos.vyos"
                "ansible.netcommon"
              ];
              groups = [ "routers" ];
              extraVars = {
                ansible_connection = "ansible.netcommon.network_cli";
                ansible_network_os = "vyos.vyos.vyos";
              };
              secretFiles.vyos_password_file = "/run/secrets/vyos-admin";
            };
            tune = {
              guest = "kept";
              playbook = ./playbooks/tune.yml;
              user = "admin";
            };
          };
        };
      };

      quiet = {
        owner = "Quiet";
        guestDefaults = {
          defaultDatastore = "s1/local";
          adoption.unrecorded = "ignore";
        };
      };

    };
    guests = {
      tmpl = {
        # A template from an image: the root disk is imported from the
        # upload, the VM is never started.
        base = {
          vmid = 9701;
          kind = "vm";
          on = "s1/n1";
          vm.template = true;
          source.image = "s1/vyos";
          networkInterfaces = [
            {
              network = "lan";
              address = "dhcp";
            }
          ];
          disks = [
            {
              role = "root";
              size = 8;
              interface = "scsi0";
            }
          ];
        };
        # Its clone, by reference: vmid and node from the template.
        router = {
          vmid = 9702;
          kind = "vm";
          on = "s1/n1";
          source.clone.guest = "tmpl/base";
          networkInterfaces = [
            {
              network = "lan";
              address = "192.0.2.91";
            }
          ];
          disks = [
            {
              role = "root";
              size = 8;
              interface = "scsi0";
            }
          ];
        };
      };

      addon = {
        router = {
          vmid = 9801;
          kind = "vm";
          on = "s1/n1";
          networkInterfaces = [
            {
              network = "lan";
              address = "192.0.2.95";
            }
          ];
          disks = [
            {
              role = "root";
              size = 8;
              interface = "scsi0";
            }
          ];
        };
        # Adopted: not rendered, so its playbook depends on nothing.
        kept = {
          vmid = 9802;
          kind = "lxc";
          mode = "adopted";
          on = "s1/n1";
          networkInterfaces = [
            {
              network = "lan";
              address = "192.0.2.96";
            }
          ];
          disks = [
            {
              role = "root";
              size = 8;
            }
          ];
        };
      };

      quiet =
        let
          guest = kind: vmid: host: extra: {
            inherit kind vmid;
            on = "s1/n1";
            networkInterfaces = [
              {
                network = "lan";
                address = "192.0.2.${toString host}";
              }
            ];
            disks = [
              (
                {
                  role = "root";
                  size = 8;
                }
                // (if kind == "vm" then { interface = "scsi0"; } else { })
              )
            ];
          }
          // extra;
        in
        {
          # The estate's default: what an import does not record is ignored.
          ct = guest "lxc" 9601 81 { };
          machine = guest "vm" 9602 82 { };
          # Beside what the guest ignores itself; cpu is not listed twice.
          mixed = guest "lxc" 9603 83 {
            ignoreChanges = [
              "operating_system"
              "cpu"
            ];
          };
          # Set back for one guest: nothing is added.
          loudct = guest "lxc" 9604 84 { adoption.unrecorded = "apply"; };
          loudvm = guest "vm" 9605 85 { adoption.unrecorded = "apply"; };
        };

      tenant.tbox = {
        vmid = 9101;
        kind = "lxc";
        on = "s1/n1";
        networkInterfaces = [
          {
            network = "lan";
            address = "192.0.2.31";
          }
        ];
        disks = [
          {
            role = "root";
            size = 8;
          }
        ];
      };

      bare.bbox = {
        vmid = 9201;
        kind = "lxc";
        on = "s1/n1";
        networkInterfaces = [
          {
            network = "lan";
            address = "192.0.2.41";
          }
        ];
        disks = [
          {
            role = "root";
            size = 8;
          }
        ];
      };

      offsite.far = {
        vmid = 9401;
        pool = "offsite/proxmox/far";
        kind = "lxc";
        on = "s2/n1";
        networkInterfaces = [
          {
            network = "lan";
            address = "198.51.100.61";
          }
        ];
        disks = [
          {
            role = "root";
            size = 8;
            datastore = "s2/local";
          }
        ];
      };

      split = {
        near = {
          vmid = 9301;
          kind = "lxc";
          on = "s1/n1";
          networkInterfaces = [
            {
              network = "lan";
              address = "192.0.2.51";
            }
          ];
          disks = [
            {
              role = "root";
              size = 8;
            }
          ];
        };
        far = {
          vmid = 9302;
          kind = "lxc";
          on = "s2/n1";
          networkInterfaces = [
            {
              network = "lan";
              address = "198.51.100.52";
            }
          ];
          disks = [
            {
              role = "root";
              size = 8;
              datastore = "s2/local";
            }
          ];
        };
      };

      gaps = {
        # An unprivileged container with a custom id mapping, the provider's
        # default console (no block) and in no pool.
        mapped = {
          vmid = 9501;
          kind = "lxc";
          on = "s1/n1";
          pool = "none";
          console.omit = true;
          idmap = [
            {
              type = "uid";
              containerId = 0;
              hostId = 100000;
              size = 1000;
            }
            {
              type = "uid";
              containerId = 1000;
              hostId = 1000;
              size = 1;
            }
            {
              type = "uid";
              containerId = 1001;
              hostId = 101001;
              size = 64535;
            }
            {
              type = "gid";
              containerId = 0;
              hostId = 100000;
              size = 1000;
            }
            {
              type = "gid";
              containerId = 1000;
              hostId = 1000;
              size = 1;
            }
            {
              type = "gid";
              containerId = 1001;
              hostId = 101001;
              size = 64535;
            }
          ];
          networkInterfaces = [
            {
              network = "lan";
              address = "192.0.2.71";
            }
          ];
          disks = [
            {
              role = "root";
              size = 8;
            }
          ];
        };
        # Raw lxc.conf lines: no companion is rendered, the description is
        # ignored (beside what the guest ignores itself).
        conf = {
          vmid = 9502;
          kind = "lxc";
          on = "s1/n1";
          description = "raw lines";
          ignoreChanges = [ "operating_system" ];
          lxcExtraConf = [ "lxc.mount.entry: /dev/net/tun dev/net/tun none bind,create=file" ];
          networkInterfaces = [
            {
              network = "lan";
              address = "192.0.2.72";
            }
            # The tag comes from the bridge (vmbr0 carries `tagged` as 7).
            {
              network = "tagged";
              address = "203.0.113.72";
            }
          ];
          disks = [
            {
              role = "root";
              size = 8;
            }
          ];
        };
        efi = {
          vmid = 9503;
          kind = "vm";
          on = "s1/n1";
          # A VM takes its name (and so its cloud-init hostname) from here.
          initialization.hostname = "efi-host";
          source.clone = {
            vmid = 9000;
            full = true;
          };
          vm = {
            bios = "ovmf";
            scsiHardware = "virtio-scsi-single";
            efiDisk = {
              fileFormat = "raw";
              type = "4m";
              preEnrolledKeys = true;
            };
            cloudInit = {
              datastore = "s1/local";
              interface = "ide2";
              upgrade = true;
            };
          };
          networkInterfaces = [
            {
              network = "lan";
              address = "192.0.2.73";
            }
          ];
          disks = [
            {
              role = "root";
              size = 16;
              interface = "scsi0";
            }
          ];
        };
      };

      mini = {
        box = {
          vmid = 9001;
          kind = "lxc";
          on = "s1/n1";
          networkInterfaces = [
            {
              network = "lan";
              address = "192.0.2.21";
            }
          ];
          disks = [
            {
              role = "root";
              size = 8;
            }
          ];
          protect = true;
        };
        machine = {
          vmid = 9002;
          kind = "vm";
          on = "s1/n1";
          networkInterfaces = [
            {
              network = "lan";
              address = "192.0.2.22";
            }
          ];
          disks = [
            {
              role = "root";
              size = 16;
              interface = "scsi0";
            }
          ];
        };
        tuned = {
          vmid = 9004;
          kind = "lxc";
          on = "s1/n1";
          networkInterfaces = [
            {
              network = "lan";
              address = "192.0.2.24";
            }
          ];
          disks = [
            {
              role = "root";
              size = 8;
            }
          ];
          lxcExtraConf = [ "lxc.apparmor.profile: unconfined" ];
        };
        legacy = {
          vmid = 9003;
          kind = "lxc";
          mode = "adopted";
          on = "s1/n1";
          networkInterfaces = [
            {
              network = "lan";
              address = "192.0.2.23";
            }
          ];
          disks = [
            {
              role = "root";
              size = 8;
            }
          ];
        };
      };
    };
  };
}
