# mkGoogleTerraform: render one estate's Google Cloud API enablement as a
# main.tf.json attrset (builtins.toJSON it). Evaluation only; nothing here
# runs tofu. The Google add-on's part of the model (modules/addons.nix):
#
#   mkGoogleTerraform { fleet; estate; } -> { terraform; provider; data; resource; locals; }
#
# One google_project_service per fleet.estates.<estate>.addons.google.
# projects.<name>.services, keyed <name>-<service without .googleapis.com>
# (the service name's dots and the project name are free text, so the key goes
# through tfName). The provider's credential is the service account key read
# through data.sops_file (lib/secrets/sops-ref.nix), never a literal.
# disable_on_destroy is always rendered, false unless the project says
# otherwise: a service dropped from the model is left enabled.
{
  lib,
  fleet,
  estate,
}:
let
  where = "mkGoogleTerraform: fleet.estates.${estate}.addons.google";
  a = fleet.estates.${estate}.addons.google or { enable = false; };
  sopsRef = import ./secrets/sops-ref.nix { inherit fleet estate where; };

  tfName = s: lib.replaceStrings [ "." ] [ "-" ] s;
  cred = sopsRef a.credentialRef;

  services = lib.listToAttrs (
    lib.concatLists (
      lib.mapAttrsToList (
        pname: pr:
        map (
          svc:
          lib.nameValuePair (tfName "${pname}-${lib.removeSuffix ".googleapis.com" svc}") {
            project = pr.projectId;
            service = svc;
            disable_on_destroy = pr.disableOnDestroy;
          }
        ) pr.services
      ) a.projects
    )
  );
in
if !a.enable then
  throw "${where}.enable is false: the estate does not switch the Google add-on on"
else
  {
    terraform.required_providers = {
      google = {
        source = "hashicorp/google";
        version = "8.10.0";
      };
      sops = {
        source = "carlpett/sops";
        version = "1.4.1";
      };
    };
    provider.google.credentials = cred.expr;
    data.sops_file.${cred.name}.source_file = cred.path;
    resource.google_project_service = services;
    locals = { };
  }
