# mkTerraform: render one estate's Proxmox guests and pools as a main.tf.json
# attrset (builtins.toJSON it). Evaluation only; nothing here runs tofu.
#
#   mkTerraform {
#     fleet;     # the checked model (config.fleet)
#     estate;    # estate name
#   } -> { terraform; provider; data; resource; locals; }
#
# Resource address: <resource type>.<guest name>. Only `managed` guests are
# rendered; the rest are listed under locals.fleet_unmanaged. An image with a
# `source` that a managed guest uses is uploaded by
# proxmox_virtual_environment_file.<image name>, and the guest depends on it,
# as a clone does on the template it names when that is a managed guest of
# this estate. Credentials are
# never literals: api_token reads placement.tokenRef, or the provider's
# tokenRef when that is null, through data.sops_file.<file alias of the ref>
# (<estate>_<alias> when the file is another estate's). The provider is
# placement.provider, or the one site's provider the guests are on.
# Companions (lxc_extra_conf) are not rendered, here or in the Pulumi program
# compiled from this: the raw lxc.conf lines of such a guest are applied by
# nothing the kit renders. The guests that have any are listed under
# locals.fleet_unrendered_companions (the Pulumi program's outputs).
#
# Add-ons (fleet.estates.<e>.addons, modules/addons.nix) add their providers
# and resources only when the estate switches them on; an estate that does not
# renders exactly as without them. Ansible (lib/ansible.nix):
# ansible_playbook.<playbook name>, after its guest. A playbook's name must not
# be the name of another resource of the render: the Pulumi program keys
# resources by name (lib/pulumi.nix), and a shared name would change the key
# of the guest.
{
  lib,
  fleet,
  estate,
}:
let
  guests = fleet.guests.${estate} or { };
  views = fleet.report.providerView.${estate} or { };
  e = fleet.estates.${estate};

  siteOf = id: lib.head (lib.splitString "/" id);
  managed = lib.filterAttrs (_: g: g.mode == "managed") guests;
  unmanaged = lib.filterAttrs (_: g: g.mode != "managed") guests;

  pools = e.pools.proxmox or { };
  poolSites = lib.mapAttrs (_: p: siteOf p.provider) pools;

  provOf = site: fleet.sites.${site}.providers.proxmox;

  where = "mkPulumi: fleet.estates.${estate}";
  sopsRef = import ./secrets/sops-ref.nix { inherit fleet estate where; };

  # One provider. With a placement it is placement.provider; without one it is
  # the proxmox provider of the one site the managed guests and pools are on.
  usedSites = lib.unique (lib.mapAttrsToList (_: g: siteOf g.on) managed ++ lib.attrValues poolSites);
  p = e.placement or null;
  providerSite =
    if p != null then
      siteOf p.provider
    else if usedSites == [ ] then
      throw "${where}: no placement, and no managed guest or pool to tell which site's provider to use"
    else if lib.length usedSites > 1 then
      throw "${where}: no placement, and its guests and pools are on more than one site (${lib.concatStringsSep ", " usedSites}); one provider can serve one site"
    else
      lib.head usedSites;

  # The credential: placement.tokenRef when set, otherwise the provider's own
  # tokenRef (the cluster's credential; whoever owns the cluster renders for
  # every estate on it). The ref sops:<estate>/<file alias>#<key> is resolved
  # through the secrets of the estate it NAMES, whichever estate is rendered.
  token =
    let
      own = (provOf providerSite).tokenRef or null;
      ref =
        if p != null && p.tokenRef != null then
          p.tokenRef
        else if own == null then
          throw "${where}: placement.tokenRef is null and so is the provider's tokenRef; one of them must name the API credential"
        else
          own;
      r = sopsRef ref;
    in
    r // { site = providerSite; };

  # Still exactly one provider: every managed guest and pool must be on its
  # site.
  onProviderSite =
    what: site:
    if site != token.site then
      throw "${where}: ${what} is on site \"${site}\" but the provider is on site \"${token.site}\"; one provider serves one site"
    else
      true;
  checked =
    lib.all (n: onProviderSite "guest ${n}" (siteOf managed.${n}.on)) (lib.attrNames managed)
    && lib.all (n: onProviderSite "pool ${n}" poolSites.${n}) (lib.attrNames pools);

  provider =
    let
      pr = provOf token.site;
    in
    {
      endpoint = pr.api;
      insecure = pr.insecureTls;
      api_token = token.expr;
    };

  lastSeg = s: lib.last (lib.splitString "/" s);

  # The uploads: every image with a source a managed guest uses. Their site is
  # the provider's (an image is of its guest's site, checked by the model).
  uploads = lib.unique (lib.concatMap (n: views.${n}.dependsOn.images) (lib.attrNames managed));
  imageOf =
    id: fleet.sites.${siteOf id}.images.${lastSeg id};
  fileResources = lib.listToAttrs (
    map (
      id:
      let
        img = imageOf id;
      in
      lib.nameValuePair (lastSeg id) {
        node_name = lastSeg img.node;
        datastore_id = lastSeg img.datastore;
        content_type = img.contentType;
        source_file = {
          # The path as text: the program is written to the store by
          # builtins.toFile, which takes no reference to a build output.
          # Evaluation does not build the image; the deploy's host must have it.
          path = builtins.unsafeDiscardStringContext (toString img.source);
          file_name = img.fileName;
        };
      }
    ) uploads
  );

  # A guest's dependencies, as Terraform addresses: the uploads of its images,
  # and the template it clones when this render creates that too.
  dependsOn =
    name:
    let
      d = views.${name}.dependsOn;
      tmpl = map lastSeg (lib.filter (id: lib.hasPrefix "${estate}/" id && managed ? ${lastSeg id}) d.guests);
    in
    map (id: "proxmox_virtual_environment_file.${lastSeg id}") d.images
    ++ map (n: "${views.${n}.resource}.${n}") tmpl;

  renderGuest =
    name:
    let
      v = views.${name};
    in
    v.args
    // lib.optionalAttrs (v.lifecycle != { }) { inherit (v) lifecycle; }
    // lib.optionalAttrs (dependsOn name != [ ]) { depends_on = dependsOn name; };

  byType = lib.foldl' (
    acc: name:
    let
      v = views.${name};
    in
    acc
    // {
      ${v.resource} = (acc.${v.resource} or { }) // {
        ${name} = renderGuest name;
      };
    }
  ) { } (lib.attrNames managed);

  poolResources = lib.mapAttrs (
    name: pool: { pool_id = name; } // lib.optionalAttrs (pool.comment != null) { inherit (pool) comment; }
  ) pools;

  ansible = import ./ansible.nix { inherit lib fleet estate; };
  others = lib.concatMap lib.attrNames (lib.attrValues byType ++ [
    pools
    fileResources
  ]);
  shared = lib.intersectLists (lib.attrNames ansible.resources) others;
  playbooks =
    if shared != [ ] then
      throw "${where}.addons.ansible.playbooks: ${lib.concatStringsSep ", " shared}: also the name of a guest, pool or upload of the estate; a playbook needs a name of its own"
    else
      ansible.resources;

  withCompanions = lib.attrNames (
    lib.filterAttrs (n: _: (views.${n}.companions or { }) != { }) guests
  );
in
{
  terraform.required_providers = {
    proxmox = {
      source = "bpg/proxmox";
      version = "0.115.0";
    };
    sops = {
      source = "carlpett/sops";
      version = "1.4.1";
    };
  }
  // ansible.requiredProviders;

  provider.proxmox = builtins.seq checked provider;

  data.sops_file.${token.name}.source_file = token.path;

  resource =
    byType
    // lib.optionalAttrs (pools != { }) {
      proxmox_virtual_environment_pool = poolResources;
    }
    // lib.optionalAttrs (fileResources != { }) {
      proxmox_virtual_environment_file = fileResources;
    }
    // lib.optionalAttrs (playbooks != { }) {
      ansible_playbook = playbooks;
    };

  locals = {
    fleet_unmanaged = lib.attrNames unmanaged;
    fleet_unrendered_companions = withCompanions;
  };
}
