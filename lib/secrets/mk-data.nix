# ADR-0009, ADR-0017: lib.secrets.mkData, a secrets data repository's whole flake.
#
#   outputs = { self, fleetkit, ... }: fleetkit.lib.secrets.mkData {
#     root = self;                 # the repository: .sops.yaml and the sops files
#     registry = ./registry.nix;   # a module: fleet.estates.<e>.secrets, fleet.sopsConfig, fleet.secretHosts
#     sync = { infisical.enable = false; bitwarden.enable = false; };
#     allow = { missingKeys = [ "path#key" ]; recipients = [ "path" ]; };   # known drift, excused
#   };
#
# Outputs:
#   root          the files' store path
#   fleetModule   the registry for fleetkit's mkFleet (which brings the schema,
#                 modules/secrets.nix; host keys come from the fleet model there)
#   report.sops   the multi-estate .sops.yaml renderer's input (sops-config.py --config)
#   report.{estates,files,placements,anchorDrift,check}
#   manifests.{infisical,bitwarden}  nixfisical / nixwarden manifest entries
#   sync          the sync settings, for the push workflow
#   checks.<system>.secrets   .sops.yaml matches the registry; no plaintext;
#                 every registry key exists in its file; every reader is a
#                 recipient; placements unique and complete (evaluation)
#   devShells.<system>.default, packages.<system>.default   the secrets CLI
{
  lib,
  systems,
  pkgsFor,
  self,
}:
{
  root,
  registry,
  sync ? { },
  allow ? { },
}:
let
  ev = import ./eval-registry.nix { inherit lib; } { inherit registry; };
  r = ev.config.fleet.report.secrets;
  inherit (ev) guard;
  at = p: if p == null then null else "${root}/${p}";
  slot = v: if v == null || builtins.isString v then v else v // { sopsFile = at v.sopsFile; };
  checkReport = ev.checkReport allow;
in
{
  inherit root;

  fleetModule = {
    _file = "fleetkit lib.secrets.mkData fleetModule";
    imports = [ registry ];
  };

  report = guard {
    sops = r.sopsConfig;
    estates = ev.config.fleet.report.sops;
    inherit (r) files placements;
    inherit (ev.config.fleet.report) anchorDrift;
    check = checkReport;
  };

  manifests = guard {
    infisical = map (
      e:
      removeAttrs e [
        "estate"
        "file"
      ]
      // {
        sopsFile = at e.sopsFile;
      }
    ) r.placements.infisical;
    bitwarden = map (
      e:
      removeAttrs e [
        "estate"
        "file"
      ]
      // {
        sopsFile = at e.sopsFile;
        username = slot e.username;
        totp = slot e.totp;
        notes = slot e.notes;
        fields = map (f: f // { value = slot f.value; }) e.fields;
      }
    ) r.placements.bitwarden;
  };

  sync = {
    infisical.enable = sync.infisical.enable or false;
    bitwarden.enable = sync.bitwarden.enable or false;
  };

  checks = lib.genAttrs systems (
    system:
    let
      pkgs = pkgsFor system;
      tools = self.packages.${system}.secrets;
    in
    {
      secrets =
        pkgs.runCommand "secrets-data-check"
          {
            nativeBuildInputs = [
              pkgs.python3
              pkgs.git
              pkgs.ripgrep
              pkgs.ssh-to-age
            ];
            report = builtins.toJSON checkReport;
            passAsFile = [ "report" ];
          }
          ''
            export HOME=$TMPDIR
            python3 ${tools}/share/fleetkit-secrets/tools/secrets-check.py --report "$reportPath" --root ${root} > $out
          '';
    }
  );

  devShells = lib.genAttrs systems (system: {
    default = self.devShells.${system}.secrets;
  });
  packages = lib.genAttrs systems (system: {
    default = self.packages.${system}.secrets;
  });
}
