{
  description = "Secrets data repository (sops files and their registry), built with fleetkit's secrets utility";

  inputs.fleetkit.url = "github:jeirslab/fleetkit/unstable";

  # ADR-0009, ADR-0017: the repository's whole flake is one mkData call.
  outputs =
    { self, fleetkit, ... }:
    fleetkit.lib.secrets.mkData {
      root = self;
      registry = ./registry.nix;
      # A push to main can ask Infisical / Vaultwarden to match the placements.
      sync = {
        infisical.enable = false;
        bitwarden.enable = false;
      };
      # Known drift, excused by `secrets check`: "path#key" missing from its file,
      # "path" not encrypted to every reader of its rule.
      allow = {
        missingKeys = [ ];
        recipients = [ ];
      };
    };
}
