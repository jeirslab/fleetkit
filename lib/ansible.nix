# The Ansible add-on's part of an estate's render: one ansible_playbook of
# the ansible/ansible provider per fleet.estates.<estate>.addons.ansible.
# playbooks.<name> (modules/addons.nix), main.tf.json shaped, for
# lib/terraform.nix to add to the guests. Evaluation only.
#
#   import ./ansible.nix { lib; fleet; estate; } -> {
#     enabled;              # addons.ansible.enable
#     requiredProviders;    # { ansible = { source; version; }; } when enabled, else { }
#     resources;            # { <name> = <ansible_playbook arguments>; }
#     needs;                # the playbooks, as strings with their store context
#   }
#
# A resource: `name` is the guest's name (the provider's one-host inventory),
# `playbook` the playbook's store path (plus `entry`), extra_vars the guest's
# address on the playbook's network (ansible_host), the SSH user
# (ansible_user), extraVars and secretFiles (paths only), depends_on the
# guest's resource when the guest is managed. replayable = true, the
# provider's default, said out loud: a refresh drops the resource from state,
# so the next `up` runs the playbook again (docs/addons.md).
#
# The playbook is rendered without its string context, as an image's source
# is: the program goes to the store through builtins.toFile, which takes no
# reference to a build output. `needs` keeps the context, and the stack
# carries it (lib/pulumi: stacks.<name>.needs), so the runner builds or
# fetches each playbook before it runs the program.
{
  lib,
  fleet,
  estate,
}:
let
  e = fleet.estates.${estate};
  a = e.addons.ansible or {
    enable = false;
    playbooks = { };
  };
  guests = fleet.guests.${estate} or { };
  views = fleet.report.providerView.${estate} or { };
  colmena = e.colmena or null;
  defaultUser = if colmena == null then "root" else colmena.targetUser or "root";

  pathOf = pb: "${pb.playbook}${lib.optionalString (pb.entry != null) "/${pb.entry}"}";

  render =
    _: pb:
    let
      g = guests.${pb.guest};
      managed = g.mode == "managed";
    in
    {
      name = pb.guest;
      playbook = builtins.unsafeDiscardStringContext (pathOf pb);
      replayable = true;
      extra_vars = {
        ansible_host = g.ipv4.${pb.network};
        ansible_user = if pb.user != null then pb.user else defaultUser;
      }
      // pb.extraVars
      // pb.secretFiles;
    }
    // lib.optionalAttrs (pb.groups != [ ]) { inherit (pb) groups; }
    // lib.optionalAttrs managed { depends_on = [ "${views.${pb.guest}.resource}.${pb.guest}" ]; };

  playbooks = if a.enable then a.playbooks else { };
in
{
  enabled = a.enable;
  requiredProviders = lib.optionalAttrs a.enable {
    ansible = {
      source = "ansible/ansible";
      version = "1.5.0";
    };
  };
  resources = lib.mapAttrs render playbooks;
  needs = lib.mapAttrsToList (_: pb: "${pb.playbook}") playbooks;
}
