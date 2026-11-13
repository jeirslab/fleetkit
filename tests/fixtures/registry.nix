# A registry with no real key in it: two estates declared by files (apps, ops),
# one kept verbatim from a hand-written .sops.yaml (legacy), placements, and
# host keys. The age recipients are placeholders of the right shape; the ssh
# key is converted by a stand-in ssh-to-age in the tests.
{ config, ... }:
let
  g = config.fleet.guests;
  q = c: "age1" + builtins.concatStringsSep "" (builtins.genList (_: c) 58);
in
{
  fleet = {
    secretHosts = {
      "apps/web" = {
        age = q "w";
      };
      "apps/db" = {
        ed25519 = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIPlaceholderPlaceholderPlaceholder00 root@db";
      };
      "lan/node1" = {
        kind = "node";
        age = q "n";
      };
    };

    sopsConfig = {
      header = "Rendered from the registry. Do not edit: run `secrets render`.";
      estates = [
        "apps"
        "ops"
        "legacy"
      ];
      protect = [ "^legacy/secrets/vpn\\.yaml$" ];
    };

    estates = {
      apps.secrets = {
        backend = "sops";
        operators.admin.age = q "z";
        files = {
          web = {
            path = "apps/web.yaml";
            keys = [
              "session_key"
              "admin/user"
              "admin/password"
            ];
            readers = [ g.apps.web.id ];
            placements = {
              session_key.infisical = {
                project = "apps";
                folder = "/web";
              };
              "admin/password".bitwarden = {
                org = "example";
                collection = "apps";
                name = "Web admin";
                username = {
                  sopsKey = "admin/user";
                };
                uris = [ "https://web.example.org" ];
              };
            };
          };
          db = {
            path = "apps/db.yaml";
            keys = [ "password" ];
            readers = [
              g.apps.db.id
              g.apps.web.id
            ];
            placements.password.infisical = {
              project = "apps";
              folder = "/db";
              groups = [ "developers" ];
            };
          };
        };
      };

      ops.secrets = {
        backend = "sops";
        operators.ops_admin.age = q "p";
        files.node = {
          path = "ops/node1.yaml";
          keys = [ "wifi/password" ];
          readers = [ config.fleet.sites.lan.nodes.node1.id ];
        };
      };

      legacy.secrets = {
        backend = "sops";
        layout = {
          prefix = "legacy_";
          root = "legacy";
          keysComment = "legacy/.sops.yaml, anchors prefixed legacy_";
          rulesComment = "legacy/.sops.yaml, in its order, re-rooted under legacy/";
          keys = [
            {
              name = "master";
              age = q "m";
            }
            {
              name = "app_host";
              age = q "a";
            }
          ];
          rules = [
            {
              pathRegex = "secrets/vpn\\.yaml$";
              anchors = [ "master" ];
            }
            {
              pathRegex = "^secrets/app\\.yaml$";
              anchors = [
                "master"
                "app_host"
              ];
            }
          ];
        };
      };
    };
  };
}
