# Minimal fleet data for tests/terraform.sh: one site, one node and the
# estate "mini" with a pool, two managed guests (lxc, vm), one managed lxc
# guest with an lxc_extra_conf companion and one adopted guest, and its own
# placement.tokenRef. Two more estates share the site's provider: "tenant"
# (placement without a tokenRef, a pool and a guest) and "bare" (no placement,
# one guest). "split" has guests on two sites and must not render; "offsite" has a placement on s1 and a guest on s2, and must not render. Generic
# names only; nothing here is a real address.
_: {
  fleet = {
    sites.s1 = {
      region = "test";
      networks.lan.cidr = "192.0.2.0/24";
      bridges.vmbr0.network = "s1/lan";
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
        ];
      };
      images.tpl = {
        datastore = "s1/local";
        contentType = "vztmpl";
        fileName = "tpl.tar.zst";
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

    };
    guests = {
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
