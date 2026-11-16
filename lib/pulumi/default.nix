# Pulumi.nix: an estate's Pulumi stacks as Nix modules. Evaluation only.
#
#   stacks { fleet; modules; specialArgs ? { }; secretRoots ? [ ]; }
#     -> { <stack> = { estate; backend; program; file; secrets; ...; }; }
#     secretRoots: directories besides the estate repo where the runner looks
#     up every stack's sops files (a tenant's source: a flake input). The same
#     as the module option `secretRoots`; each stack carries the list.
#   fromModel { estate; github ? false; google ? false; adopt ? true; }
#     -> a module: the stack <estate>-guests (or <estate>-github, or
#        <estate>-google for the Google add-on: no stack at all while the
#        estate has not switched it on) rendered from the model, with its
#        backend from fleet.backends. adopt: each
#        resource's `adopt` (the id of the existing resource it describes) is
#        computed from the model. The ids are data beside the program
#        (<stack>.adoptIds, <stack>.adoptUnresolved), never part of it, so
#        they are computed by default; false leaves every `adopt` unset.
#        The guests stack also `needs` the playbooks of the estate's Ansible
#        add-on (lib/ansible.nix), for the runner to realise before it runs.
#
# An estate repo's pulumi.nix imports fromModel for what the model describes
# and adds what it does not, as ordinary resources:
#
#   { fleetkit, ... }: {
#     imports = [ (fleetkit.lib.pulumi.fromModel { estate = "homelab"; }) ];
#     stacks.homelab-guests.resources.www = { type = "..."; properties = { ... }; };
#   }
#
# The modules get `fleet` (the checked model) and `fleetkit` as arguments.
{ lib, self }:
let
  stacks =
    {
      fleet,
      modules,
      specialArgs ? { },
      secretRoots ? [ ],
    }:
    (lib.evalModules {
      modules = [
        ./options.nix
        { inherit secretRoots; }
      ]
      ++ modules;
      specialArgs = {
        inherit fleet;
        fleetkit = self;
      }
      // specialArgs;
    }).config.stacks;

  fromModel =
    {
      estate,
      github ? false,
      google ? false,
      adopt ? true,
    }:
    { fleet, ... }:
    let
      e = fleet.estates.${estate};
      # A Google stack of an estate that has not switched the add-on on is no
      # stack: the module is empty (the render, which refuses, is not evaluated).
      off = google && !(e.addons.google.enable or false);
      p =
        if google then
          self.lib.mkGooglePulumi { inherit fleet estate; }
        else if github then
          self.lib.mkGithubPulumi { inherit fleet estate adopt; }
        else
          self.lib.mkPulumi { inherit fleet estate adopt; };
      ansible = import ../ansible.nix { inherit lib fleet estate; };
      backendId = if github then e.git.backend or e.backend or null else e.backend or null;
    in
    lib.optionalAttrs (!off) {
      stacks.${p.name} = {
        inherit estate;
        inherit (p) packages variables outputs;
        inherit (p) resources;
        needs = lib.mkIf (!github && !google) ansible.needs;
        backend = lib.mkIf (backendId != null) (
          import ./backend.nix { inherit lib fleet estate; } {
            id = backendId;
            project = p.name;
          }
        );
      };
    };
in
{
  inherit stacks fromModel;
}
