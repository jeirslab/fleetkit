# Pulumi.nix: an estate's Pulumi stacks as Nix modules. Evaluation only.
#
#   stacks { lib; fleet; modules; specialArgs ? { }; }
#     -> { <stack> = { estate; backend; program; file; secrets; ...; }; }
#   fromModel { estate; github ? false; adopt ? true; }
#     -> a module: the stack <estate>-guests (or <estate>-github) rendered from
#        the model, with its backend from fleet.backends. adopt: each
#        resource's `adopt` (the id of the existing resource it describes) is
#        computed from the model. The ids are data beside the program
#        (<stack>.adoptIds, <stack>.adoptUnresolved), never part of it, so
#        they are computed by default; false leaves every `adopt` unset.
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
    }:
    (lib.evalModules {
      modules = [ ./options.nix ] ++ modules;
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
      adopt ? true,
    }:
    { fleet, ... }:
    let
      p =
        if github then
          self.lib.mkGithubPulumi { inherit fleet estate adopt; }
        else
          self.lib.mkPulumi { inherit fleet estate adopt; };
      e = fleet.estates.${estate};
      backendId = if github then e.git.backend or e.backend or null else e.backend or null;
    in
    {
      stacks.${p.name} = {
        inherit estate;
        inherit (p) packages variables outputs;
        inherit (p) resources;
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
