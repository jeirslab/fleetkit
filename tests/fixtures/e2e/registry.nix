# tests/e2e.sh's data repository: host keys and the operator's recipient are
# generated at test time into hosts.json and operators.json.
{ lib, config, ... }:
let
  g = config.fleet.guests;
in
{
  fleet = {
    secretHosts = lib.importJSON ./hosts.json;
    sopsConfig = {
      header = "e2e";
      protect = [ "^secrets/vpn\\.yaml$" ];
    };
    estates.apps.secrets = {
      backend = "sops";
      operators = lib.importJSON ./operators.json;
      files = {
        web = {
          path = "secrets/web.yaml";
          keys = [
            "token"
            "db/password"
          ];
          readers = [ g.apps.web.id ] ++ lib.optional (builtins.pathExists ./web-reads-db) g.apps.db.id;
          placements.token.infisical.project = "apps";
        };
        db = {
          path = "secrets/db.yaml";
          keys = [ "password" ] ++ lib.optional (builtins.pathExists ./db-extra-key) "extra";
          readers = [ g.apps.db.id ];
        };
        vpn = {
          path = "secrets/vpn.yaml";
          keys = [ "profile" ];
          readers = [ g.apps.web.id ];
        };
      };
    };
  };
}
