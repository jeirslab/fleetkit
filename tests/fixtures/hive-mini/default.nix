# hive-mini: the smallest fleet model that mkSystems and mkHive can use.
# One site, one proxmox node plus two nixos machines (one names a module),
# one network, one proxmox provider, one estate with
# `colmena.targetUser`, one principal with a key and a grant, two lxc guests.
# Public-safe: example names and 10.0.0.x addresses only.
_: {
  fleetkit.keys = {
    ageDir = "keys/age";
    sshDir = "keys/ssh";
  };

  fleet = {
    sites.site1 = {
      region = "eu";
      networks.lan = {
        cidr = "10.0.0.0/24";
        dns = [ "10.0.0.1" ];
      };
      bridges.vmbr0.network = "site1/lan";
      nodes = {
        pve1 = {
          osType = "debian-pve";
          address = "10.0.0.10";
        };
        # A bare-metal NixOS machine that names its module, and one that does
        # not (so mkSystems / mkHive with `site` must leave it out).
        box1 = {
          osType = "nixos";
          address = "10.0.0.11";
          nixos.module = ./node.nix;
        };
        box2 = {
          osType = "nixos";
          address = "10.0.0.12";
        };
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
      images.base = {
        datastore = "site1/local";
        contentType = "vztmpl";
        fileName = "base.tar.zst";
        build.method = "nix";
      };
      providers.proxmox = {
        api = "https://10.0.0.10:8006/";
        tokenRef = "sops:example/main#token";
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
            bridge = "site1/vmbr0";
          };
          operatingSystem = {
            type = "nixos";
            templateFileId = "local:vztmpl/base.tar.zst";
          };
          startOnBoot = true;
          started = true;
        };
      };
    };

    operators = {
      principals.alice = {
        kind = "person";
        keys.ssh.laptop = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIExampleExampleExampleExampleExampleExample alice@example";
      };
      roles.admin = {
        account = "admin";
        can = [
          "login"
          "sudo"
          "deploy"
        ];
      };
      grants.alice-admin = {
        principals = [ "alice" ];
        role = "admin";
      };
    };

    estates.example = {
      owner = "Example";
      colmena.targetUser = "deploy";
      secrets = {
        backend = "sops";
        files.main = {
          path = "secrets/main.yaml";
          keys = [ "token" ];
        };
      };
    };

    guests.example =
      let
        guest = vmid: address: tags: {
          inherit vmid tags;
          kind = "lxc";
          on = "site1/pve1";
          nixos.module = ./guest.nix;
          networkInterfaces = [
            {
              network = "lan";
              inherit address;
            }
          ];
          disks = [
            {
              role = "root";
              size = 8;
              datastore = "site1/local";
            }
          ];
        };
      in
      {
        web = guest 101 "10.0.0.21" [
          "web"
          "frontend"
        ];
        db = guest 102 "10.0.0.22" [ "db" ];
      };
  };
}
