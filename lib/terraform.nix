# mkTerraform: render one estate's Proxmox guests and pools as a main.tf.json
# attrset (builtins.toJSON it). Evaluation only; nothing here runs tofu.
#
#   mkTerraform {
#     fleet;     # the checked model (config.fleet)
#     estate;    # estate name
#   } -> { terraform; provider; data; resource; locals; }
#
# Resource address: <resource type>.<guest name>. Only `managed` guests are
# rendered; the rest are listed under locals.fleet_unmanaged. Credentials are
# never literals: api_token reads placement.tokenRef, or the provider's
# tokenRef when that is null, through data.sops_file.<file alias of the ref>
# (<estate>_<alias> when the file is another estate's). The provider is
# placement.provider, or the one site's provider the guests are on.
# Companions (lxc_extra_conf) are not rendered yet; the guests that have any
# are listed under locals.fleet_unrendered_companions.
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

  where = "mkTerraform: fleet.estates.${estate}";

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
      m = builtins.match "sops:([^/#]+)/([^#]+)#(.+)" ref;
      owner = builtins.elemAt m 0;
      file = builtins.elemAt m 1;
      files = fleet.estates.${owner}.secrets.files or { };
    in
    if m == null then
      throw "${where}: tokenRef \"${ref}\" is not sops:<estate>/<file>#<key>"
    else if !(fleet.estates ? ${owner}) then
      throw "${where}: tokenRef \"${ref}\" names estate \"${owner}\", which is not declared"
    else if !(files ? ${file}) then
      throw "${where}: tokenRef \"${ref}\": fleet.estates.${owner}.secrets.files has no \"${file}\""
    else
      {
        # data.sops_file is keyed by the alias; by <estate>_<alias> when the
        # file belongs to another estate, so the two cannot collide.
        name = if owner == estate then file else "${owner}_${file}";
        inherit (files.${file}) path;
        # The ref names the key as a path into a nested document, slash
        # separated; carlpett/sops flattens nested keys with "." (sops/flatten.go
        # v1.4.1), so data.sops_file.<alias>.data is indexed in that form.
        key = builtins.replaceStrings [ "/" ] [ "." ] (builtins.elemAt m 2);
        site = providerSite;
      };

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
      api_token = "\${data.sops_file.${token.name}.data[\"${token.key}\"]}";
    };

  renderGuest =
    name:
    let
      v = views.${name};
    in
    v.args
    // lib.optionalAttrs (v.lifecycle != { }) { inherit (v) lifecycle; };

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

  poolResources = lib.mapAttrs (name: _: { pool_id = name; }) pools;

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
  };

  provider.proxmox = builtins.seq checked provider;

  data.sops_file.${token.name}.source_file = token.path;

  resource =
    byType
    // lib.optionalAttrs (pools != { }) {
      proxmox_virtual_environment_pool = poolResources;
    };

  locals = {
    fleet_unmanaged = lib.attrNames unmanaged;
    fleet_unrendered_companions = withCompanions;
  };
}
