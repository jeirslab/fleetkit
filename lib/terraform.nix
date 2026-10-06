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
# never literals: api_token reads the estate's placement.tokenRef through
# data.sops_file.<file alias of the ref>. Companions (lxc_extra_conf) are not
# rendered yet; the guests that have any are listed under
# locals.fleet_unrendered_companions.
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

  # The credential is always in the estate's OWN secrets
  # (sops:<estate>/<file alias>#<key>, resolved through this estate's
  # secrets.files only): placement.tokenRef, or, when that is null, the
  # provider's tokenRef. A provider is one per cluster and the estates on it
  # share it, each with its own token; the provider's own token only resolves
  # for the estate whose secrets hold it, so a tenant never renders it.
  where = "mkTerraform: fleet.estates.${estate}";
  token =
    let
      p = e.placement or null;
      ref =
        if p == null then
          throw "${where}.placement is null; it must name the provider and the tokenRef this estate uses"
        else if p.tokenRef != null then
          p.tokenRef
        else
          # The cluster's own token, for the estate that owns the cluster: the
          # same-estate check below refuses it for anyone else.
          let
            own = (provOf (siteOf p.provider)).tokenRef or null;
          in
          if own == null then
            throw "${where}.placement.tokenRef is null and so is the provider's tokenRef; one of them must name the API credential"
          else
            own;
      m = builtins.match "sops:([^/#]+)/([^#]+)#(.+)" ref;
      file = builtins.elemAt m 1;
      files = e.secrets.files or { };
    in
    if m == null then
      throw "${where}.placement.tokenRef \"${ref}\" is not sops:<estate>/<file>#<key>"
    else if builtins.elemAt m 0 != estate then
      throw "${where}.placement.tokenRef \"${ref}\" names estate \"${builtins.elemAt m 0}\"; the credential must be in this estate's own secrets (set placement.tokenRef)"
    else if !(files ? ${file}) then
      throw "${where}.placement.tokenRef \"${ref}\": secrets.files has no \"${file}\""
    else
      {
        inherit file;
        inherit (files.${file}) path;
        key = builtins.elemAt m 2;
        site = siteOf p.provider;
      };

  # One provider: the estate's placement provider, with the estate's token.
  # Everything rendered must be on its site; a guest or a pool elsewhere would
  # need a credential this estate was not given.
  onPlacementSite =
    what: site:
    if site != token.site then
      throw "${where}: ${what} is on site \"${site}\" but placement.provider is on site \"${token.site}\"; placement.tokenRef is only valid there"
    else
      true;
  checked =
    lib.all (n: onPlacementSite "guest ${n}" (siteOf managed.${n}.on)) (lib.attrNames managed)
    && lib.all (n: onPlacementSite "pool ${n}" poolSites.${n}) (lib.attrNames pools);

  provider =
    let
      pr = provOf token.site;
    in
    {
      endpoint = pr.api;
      insecure = pr.insecureTls;
      api_token = "\${data.sops_file.${token.file}.data[\"${token.key}\"]}";
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

  data.sops_file.${token.file}.source_file = token.path;

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
