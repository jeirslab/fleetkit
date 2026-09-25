{ config, lib, stackId ? null, ... }:

# Emits Cloudflare DNS resources for cloudflare-zone entries belonging
# to the current stack. For each zone entry, emits:
#   - data.cloudflare_zone.<safe_zone_name>   — zone ID lookup
#   - resource.cloudflare_record.<sub>_<zone>  — one A record per entry in `records`
#
# `records` maps subdomain → fleet.compute key; the emitter resolves
# fleet.compute.<key>.internal_ip for the record content.

let
  resourcesInStack = lib.filterAttrs
    (_: r: "${r.env}.${r.stack}" == stackId)
    config.fleet.resources;

  byKind = lib.groupBy (r: r.kind)
    (lib.mapAttrsToList
      (name: meta: meta // { _name = name; })
      resourcesInStack);

  zoneEntries = byKind."cloudflare-zone" or [];

  # "*" → "wildcard" so a wildcard record key (name = "*") still yields a
  # valid Terraform resource identifier (wildcard_<zone>); the record's
  # `name` field stays the raw subdomain ("*"), giving *.<zone> in Cloudflare.
  safeName = s: lib.replaceStrings [ "-" "." " " "*" ] [ "_" "_" "_" "wildcard" ] s;

  # Value may be a fleet.compute key (resolved to internal_ip) or a
  # literal IP string (used as-is, for WAN IPs not in fleet.compute).
  # Anything else aborts the eval: the old silent fallthrough emitted
  # retired fleet keys verbatim as A-record content, publishing broken
  # records on apply (INFRA-110).
  looksLikeIp = s: builtins.match "[0-9]+\\.[0-9]+\\.[0-9]+\\.[0-9]+" s != null;
  resolveIp = value:
    if builtins.hasAttr value config.fleet.compute
    then config.fleet.compute.${value}.internal_ip
    else if looksLikeIp value
    then value
    else throw "cloudflare.nix: DNS record value '${value}' is neither a fleet.compute key nor a literal IPv4 — fix the records map (INFRA-110)";

  mkZoneData = e: lib.nameValuePair (safeName e.zone_name) {
    name = e.zone_name;
  };

  # A record value is either:
  #   - a string  → an A record (fleet.compute key resolved to internal_ip,
  #                  or a literal IP used as-is); the common case.
  #   - an attrset → an explicit record: { type; content; ttl?; proxied?; }.
  #                  Used for the acme-dns NS delegation, its glue A record,
  #                  and the per-host _acme-challenge CNAMEs (ADR-026).
  mkZoneRecords = e:
    let zoneSafe = safeName e.zone_name;
        zoneIdRef = "\${data.cloudflare_zone.${zoneSafe}.id}";
    in lib.mapAttrs' (subdomain: value:
      let spec = builtins.isAttrs value; in
      # The map key is the Terraform resource id; the record NAME defaults to it
      # but a spec may set `name` explicitly so several records can share one name
      # (e.g. an apex with a verification TXT + an SPF TXT + two MX). `priority`
      # is emitted only when present (MX/SRV).
      lib.nameValuePair "${safeName subdomain}_${zoneSafe}" ({
        zone_id         = zoneIdRef;
        name            = if spec then (value.name or subdomain) else subdomain;
        type            = if spec then value.type else "A";
        content         = if spec then value.content else resolveIp value;
        ttl             = if spec then (value.ttl or 1) else 1;     # 1 = Automatic
        proxied         = if spec then (value.proxied or false) else false;  # DNS-only
        allow_overwrite = true;   # Take ownership of pre-existing records
      } // lib.optionalAttrs (spec && value ? priority) { priority = value.priority; })
    ) e.records;

  allZoneData = lib.listToAttrs (map mkZoneData zoneEntries);
  allRecords  = lib.foldl' (acc: e: acc // mkZoneRecords e) {} zoneEntries;

in {
  config = lib.mkIf (stackId != null && zoneEntries != []) {
    data.cloudflare_zone     = allZoneData;
    resource.cloudflare_record = allRecords;
  };
}
