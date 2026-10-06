# Minimal fleet data for tests/terraform.sh: one site, one node, one estate
# with a pool, two managed guests (lxc, vm), one managed lxc guest with an
# lxc_extra_conf companion and one adopted guest. Generic
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
        # Deliberately not the estate's placement.tokenRef: the render must
        # use the placement one.
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

    estates.mini = {
      owner = "Mini";
      secrets = {
        backend = "sops";
        files.tf = {
          path = "secrets/tf.json";
          keys = [
            "pve-token"
            "site-token"
          ];
        };
      };
      placement = {
        provider = "s1/proxmox";
        pool = "mini/proxmox/main";
        tokenRef = "sops:mini/tf#pve-token";
      };
      pools.proxmox.main.provider = "s1/proxmox";
      guestDefaults.defaultDatastore = "s1/local";
    };

    guests.mini = {
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
}
