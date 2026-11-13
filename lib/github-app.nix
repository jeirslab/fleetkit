# mkGithubAppManifest: the GitHub App manifest for one org, as an attrset
# (builtins.toJSON it). Pure; nothing here talks to GitHub.
#
#   mkGithubAppManifest {
#     org;                                  # the org (or user) the App is created under
#     tiers ? [ "pipeline" "terraform-admin" ];
#     name ? null;                          # default "<org>-fleet"
#     redirectUrl ? null;                   # where GitHub sends the one-time code
#     description ? null;                   # default: the data's description
#     extraPermissions ? { };               # { <permission> = level; } on top of the tiers
#   } -> { name; url; description; public; hook_attributes;
#          default_events; default_permissions; redirect_url?; }
#
# The data is github/app-manifest.nix. Tiers merge by taking the higher level
# per permission (read < write < admin), so a tier set is order independent.
# terraform-admin always includes pipeline: selecting it alone renders the
# pipeline permissions too. `runners` is meant to stand alone (an App of its
# own for a runner host). extraPermissions merge the same way (higher level
# wins) and are checked by tests/github_app.sh against the pinned list.
{
  lib,
  org,
  tiers ? [
    "pipeline"
    "terraform-admin"
  ],
  name ? null,
  redirectUrl ? null,
  description ? null,
  extraPermissions ? { },
}:
let
  data = import ../github/app-manifest.nix;
  rank = {
    read = 1;
    write = 2;
    admin = 3;
  };
  appName = if name != null then name else lib.replaceStrings [ "%s" ] [ org ] data.namePattern;
  unknown = lib.filter (t: !(data.tiers ? ${t})) tiers;
  effectiveTiers = lib.unique (
    lib.optional (lib.elem "terraform-admin" tiers) "pipeline" ++ tiers
  );
  higher = a: b: if rank.${a} >= rank.${b} then a else b;
  mergePerms = acc: add: acc // lib.mapAttrs (k: v: if acc ? ${k} then higher acc.${k} v else v) add;
  permissions = mergePerms (lib.foldl' (acc: t: mergePerms acc data.tiers.${t}) { } effectiveTiers) extraPermissions;
  badLevels = lib.filter (k: !(rank ? ${extraPermissions.${k}})) (lib.attrNames extraPermissions);
in
assert lib.assertMsg (org != "") "mkGithubAppManifest: org must not be empty";
assert lib.assertMsg (tiers != [ ]) "mkGithubAppManifest: tiers must not be empty";
assert lib.assertMsg (unknown == [ ])
  "mkGithubAppManifest: unknown tier(s) ${lib.concatStringsSep ", " unknown}; known: ${lib.concatStringsSep ", " (lib.attrNames data.tiers)}";
assert lib.assertMsg (badLevels == [ ])
  "mkGithubAppManifest: extraPermissions level must be read, write or admin: ${lib.concatStringsSep ", " badLevels}";
assert lib.assertMsg (
  builtins.stringLength appName <= data.nameMaxLength
) "mkGithubAppManifest: App name \"${appName}\" is longer than ${toString data.nameMaxLength} characters";
{
  name = appName;
  url = "https://github.com/${org}";
  description = if description != null then description else data.description;
  inherit (data) public;
  hook_attributes = {
    url = "https://github.com/${org}";
    active = data.hookActive;
  };
  default_events = data.defaultEvents;
  default_permissions = permissions;
}
// lib.optionalAttrs (redirectUrl != null) { redirect_url = redirectUrl; }
