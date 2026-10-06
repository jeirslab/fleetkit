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

  # Sites whose proxmox provider this estate's rendered guests use (through
  # each guest's node).
  sites = lib.unique (lib.mapAttrsToList (_: g: siteOf g.on) managed);
  multi = lib.length sites > 1;
  provOf = site: fleet.sites.${site}.providers.proxmox;

  # The credential is the estate's own placement.tokenRef and nothing else:
  # sops:<estate>/<file alias>#<key>, resolved through this estate's
  # secrets.files only. There is no fallback to a site provider's tokenRef,
  # which may name another estate's secrets.
  where = "mkTerraform: fleet.estates.${estate}";
  token =
    let
      p = e.placement or null;
      ref =
        if p == null then
          throw "${where}.placement is null; it must name the provider and the tokenRef this estate uses"
        else if p.tokenRef == null then
          throw "${where}.placement.tokenRef is null; the estate's own API credential is required"
        else
          p.tokenRef;
      m = builtins.match "sops:([^/#]+)/([^#]+)#(.+)" ref;
      file = builtins.elemAt m 1;
      files = e.secrets.files or { };
    in
    if m == null then
      throw "${where}.placement.tokenRef \"${ref}\" is not sops:<estate>/<file>#<key>"
    else if builtins.elemAt m 0 != estate then
      throw "${where}.placement.tokenRef \"${ref}\" names estate \"${builtins.elemAt m 0}\"; it must be in this estate's own secrets"
    else if !(files ? ${file}) then
      throw "${where}.placement.tokenRef \"${ref}\": secrets.files has no \"${file}\""
    else
      {
        inherit file;
        inherit (files.${file}) path;
        key = builtins.elemAt m 2;
        site = siteOf p.provider;
      };

  # The token is valid on the placement provider only.
  tokenFor =
    site:
    if site != token.site then
      throw "${where}: a guest is on site \"${site}\" but placement.provider is on site \"${token.site}\"; placement.tokenRef is only valid there"
    else
      token;

  alias = site: lib.replaceStrings [ "-" ] [ "_" ] site;
  providerRef = site: "proxmox.${alias site}";
  withProvider = site: lib.optionalAttrs multi { provider = providerRef site; };

  providerFor =
    site:
    let
      pr = provOf site;
      t = tokenFor site;
    in
    {
      endpoint = pr.api;
      insecure = pr.insecureTls;
      api_token = "\${data.sops_file.${t.file}.data[\"${t.key}\"]}";
    }
    // lib.optionalAttrs multi { alias = alias site; };

  renderGuest =
    name: g:
    let
      v = views.${name};
    in
    v.args
    // withProvider (siteOf g.on)
    // lib.optionalAttrs (v.lifecycle != { }) { inherit (v) lifecycle; };

  byType = lib.foldl' (
    acc: name:
    let
      v = views.${name};
    in
    acc
    // {
      ${v.resource} = (acc.${v.resource} or { }) // {
        ${name} = renderGuest name guests.${name};
      };
    }
  ) { } (lib.attrNames managed);

  poolResources = lib.mapAttrs (
    name: _: { pool_id = name; } // withProvider poolSites.${name}
  ) pools;

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

  provider.proxmox =
    let
      l = map providerFor sites;
    in
    if multi then l else lib.head (l ++ [ { } ]);

  data.sops_file = lib.listToAttrs (
    map (
      s:
      let
        t = tokenFor s;
      in
      lib.nameValuePair t.file { source_file = t.path; }
    ) sites
  );

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
