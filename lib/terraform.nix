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
# never literals: api_token reads a key of a sops file through the sops
# provider. Companions (lxc_extra_conf) are not rendered yet; the guests that
# have any are listed under locals.fleet_unrendered_companions.
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

  # Sites whose proxmox provider this estate uses (guest nodes and pools).
  sites = lib.unique (
    lib.mapAttrsToList (_: g: siteOf g.on) managed ++ lib.attrValues poolSites
  );
  multi = lib.length sites > 1;
  provOf = site: fleet.sites.${site}.providers.proxmox;

  # The estate's own tokenRef wins on the provider it is placed on.
  tokenRefOf =
    site:
    let
      p = e.placement or null;
    in
    if p != null && p.tokenRef != null && siteOf p.provider == site then
      p.tokenRef
    else
      (provOf site).tokenRef;

  # sops:<estate>/<file>#<key> -> { path; key; }
  resolve =
    ref:
    let
      m = builtins.match "sops:([^/#]+)/([^#]+)#(.+)" ref;
      est = builtins.elemAt m 0;
      file = builtins.elemAt m 1;
    in
    {
      path = fleet.estates.${est}.secrets.files.${file}.path;
      key = builtins.elemAt m 2;
    };

  alias = site: lib.replaceStrings [ "-" ] [ "_" ] site;
  providerRef = site: "proxmox.${alias site}";
  withProvider = site: lib.optionalAttrs multi { provider = providerRef site; };

  providerFor =
    site:
    let
      pr = provOf site;
      t = resolve (tokenRefOf site);
    in
    {
      endpoint = pr.api;
      insecure = pr.insecureTls;
      api_token = "\${data.sops_file.${alias site}.data[\"${t.key}\"]}";
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
    lib.filterAttrs (n: _: (views.${n}.companions or { }) != { }) managed
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
      s: lib.nameValuePair (alias s) { source_file = (resolve (tokenRefOf s)).path; }
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
