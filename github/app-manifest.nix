# The GitHub App an estate's pipeline uses, as data. lib/github-app.nix turns
# this into the manifest JSON GitHub's "register a GitHub App from a manifest"
# flow takes (docs/github-app.md). Keys are GitHub's manifest names; every
# permission key is checked by tests/github_app.sh against github/permission-names.txt,
# a pinned copy of GitHub's server-to-server permission list.
#
# Tiers are additive: a later tier raises the level of a permission it names,
# and adds the ones it does not share. The App is per org, private, no webhook.
{
  # `<org>-fleet` unless the caller names the App; GitHub caps names at 34.
  namePattern = "%s-fleet";
  nameMaxLength = 34;

  description = "Fleet pipeline and infrastructure automation (fleetkit).";

  # No webhook, no event subscriptions.
  hookActive = false;
  defaultEvents = [ ];
  public = false;

  tiers = {
    # What CI needs: read code, report status, comment, dispatch workflows.
    pipeline = {
      metadata = "read";
      contents = "read";
      statuses = "write";
      pull_requests = "write";
      issues = "write";
      actions = "write";
    };
    # What OpenTofu needs to manage the org and its repositories.
    terraform-admin = {
      administration = "write";
      secrets = "write";
      actions_variables = "write";
      workflows = "write";
      contents = "write";
      members = "write";
      organization_administration = "write";
    };
  };
}
