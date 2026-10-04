{
  # Minimal fleetkit consumer. Copy with:
  #   nix flake init -t <fleetkit-ref>#minimal
  #
  # Layout:
  #   flake.nix        — this file: mkFleet wiring + output re-export
  #   fleet/           — YOUR manifest: settings, providers, network,
  #                      users, dns data, and one file per host
  #   nix/secrets/     — SOPS store (create with `sops nix/secrets/secrets.yaml`)

  inputs = {
    fleetkit.url = "github:REPLACE-ME/fleetkit";
    nixpkgs.follows = "fleetkit/nixpkgs";
  };

  outputs = { self, fleetkit, nixpkgs }:
  let
    fleet = fleetkit.lib.mkFleet {
      # Everything environment-specific enters through these arguments.
      modules = [ ./fleet ];
      # Tofu state backend comes from fleet.settings.backend (ADR-097) —
      # declared with the rest of your settings, not here.
      # NixOS modules applied to every host — app flakes, sops defaults,
      # module-args. Start empty.
      globalModules = [ ];
      # Per-host flake-input modules, e.g. { myhost = [ inputs.microvm.nixosModules.host ]; }
      hostExtraModules = { };
      # Opt-in add-ons (see add-ons/ in fleetkit): hand mkFleet the flake input of
      # each add-on you want, then enable it per host. Nothing here is fetched or
      # locked unless you add the input. First add-on: numtide/llm-agents.nix
      # (AI coding agents): add `llm-agents.url = "github:numtide/llm-agents.nix";`
      # to this flake's inputs, then
      # addons = { llm-agents = inputs.llm-agents; };
    };
  in
  {
    inherit (fleet) colmena nixosConfigurations fleetManifest fleetAccess;

    packages.x86_64-linux = fleet.packages // {
      # The operator CLI, re-exported so `nix run .#fleet` works here.
      fleet = fleetkit.packages.x86_64-linux.fleet;
      default = fleetkit.packages.x86_64-linux.fleet;
    };

    devShells.x86_64-linux.default = fleetkit.devShells.x86_64-linux.default;
  };
}
