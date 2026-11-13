# The registry: which sops files exist, the key NAMES in each, who reads them,
# and where a value is mirrored (Infisical, Bitwarden). Public material only:
# never a value, never a private key.
#
# `secrets render` writes .sops.yaml from it; `secrets check` proves the files
# agree with it; `secrets host add` records a host's key in hosts.json.
# `config` names the readers (config.fleet.guests.<estate>.<name>.id) once a
# file is declared.
# deadnix: skip
{ lib, config, ... }:
{
  fleet = {
    # Host keys: "<estate>/<name>" = { ed25519 = "ssh-ed25519 AAAA..."; }
    # (or age = "age1..."; kind = "node" for "<site>/<name>").
    secretHosts = lib.importJSON ./hosts.json;

    sopsConfig.header = "Rendered from registry.nix. Do not edit: run `secrets render`.";

    estates.example.secrets = {
      backend = "sops";
      # People or machines that are not hosts and must decrypt and re-key.
      operators = {
        # admin.age = "age1...";
      };
      files = {
        # app = {
        #   path = "secrets/app.yaml";
        #   keys = [ "db/password" "session_key" ];
        #   readers = [ config.fleet.guests.example.app.id ];
        #   placements."db/password".infisical = { project = "app"; folder = "/db"; };
        # };
      };
    };
  };
}
