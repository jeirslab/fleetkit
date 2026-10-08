# The GitHub App manifest (`lib.mkGithubAppManifest`)

One declaration, `github/app-manifest.nix`, describes the GitHub App an
estate's pipeline uses. `lib.mkGithubAppManifest { org; tiers ? [ "pipeline"
"terraform-admin" ]; name ? null; redirectUrl ? null; }` renders it as the
manifest attrset (`builtins.toJSON` it). Pure evaluation; the kit never talks
to GitHub. Creating the App from the manifest is a separate step (the
bootstrap command, its own issue).

## The manifest flow

Checked against GitHub's documentation, "Registering a GitHub App from a
manifest", on 2026-10-07. The manifest is JSON sent as the form field
`manifest` (plus an optional `state`) to
`https://github.com/organizations/<org>/settings/apps/new`
(`https://github.com/settings/apps/new` for a user account). The owner
confirms on GitHub; GitHub then redirects to `redirect_url` with a temporary
`code`, which is exchanged once, via `POST /app-manifests/{code}/conversions`,
for the App id, client secret, webhook secret and private key. Accepted
parameters: `name`, `url` (required), `hook_attributes`, `redirect_url`,
`callback_urls`, `setup_url`, `description`, `public`, `default_events`,
`default_permissions`, `request_oauth_on_install`, `setup_on_update`.

What the kit sets: `name` is `<org>-fleet` (override with `name`; GitHub caps
names at 34 characters), `url` is the org page, `public = false`, no
webhook (`hook_attributes.active = false`), no events. `hook_attributes.url`
is still sent because the field requires one; it is never called.
`redirect_url` is only set when you pass `redirectUrl`.

Every permission key below was checked against GitHub's server-to-server
permission list, pinned in `github/permission-names.txt` (`actions`, `administration`, `contents`, `issues`,
`members`, `metadata`, `organization_administration`, `pull_requests`,
`secrets`, `statuses`, `actions_variables`, `workflows`).

### Rendering the manifest in Actions

`.github/workflows/github-app-manifest.yml` is a reusable workflow that renders
the manifest for one organisation from a pinned commit of the kit and
publishes it as the artifact `github-app-manifest`, with the permissions in
the job summary. It takes no secret: the manifest is a name, a URL and the
permissions asked for. A caller:

```yaml
name: github-app-manifest
on: workflow_dispatch
permissions: {}
jobs:
  manifest:
    uses: jeirslab/fleetkit/.github/workflows/github-app-manifest.yml@<40-hex commit>
    with:
      org: acme
      kit_ref: <the same 40-hex commit>
      runs_on: '["ubuntu-latest"]'
```

Registering the App stays a step a person runs
(`docs/github-app-bootstrap.md`): an organisation owner confirms it in a
browser and the answer is the App's private key, which must not pass through
a workflow log or an artifact.

## An App is per org

The App is private, owned by the org that registered it, and installed by an
org owner on that org's repositories. A manifest registers the App only;
installing it is a second, manual step, see below. Nothing in the kit has
access to another org's App.

## Permissions, by tier

Tiers add up. `pipeline` is for CI; `terraform-admin` is for OpenTofu
managing the org. Selecting both takes the higher level per permission.

### pipeline

| Permission | Level | Why |
| ---------- | ----- | --- |
| `metadata` | read | Required by GitHub for every App. |
| `contents` | read | Check out the repositories the pipeline builds. |
| `statuses` | write | Report the gate's result on a commit. |
| `pull_requests` | write | Comment on and label pull requests. |
| `issues` | write | Open and update issues the pipeline raises. |
| `actions` | write | Dispatch and re-run workflows. |

### terraform-admin (adds)

| Permission | Level | Why |
| ---------- | ----- | --- |
| `administration` | write | Create and configure repositories; register repo runners. |
| `secrets` | write | Repository Actions secrets. |
| `actions_variables` | write | Repository Actions variables. |
| `workflows` | write | Write files under `.github/workflows/`. |
| `contents` | write | Commit managed files (raises the pipeline level). |
| `members` | write | Org membership and teams. |
| `organization_administration` | write | Org settings. |

## Installing the App (second step)

Registering from the manifest does not give the App access to anything. An
org owner must install it: GitHub, org settings, Installed GitHub Apps (or the
App's own page, Install App), choose the org, and select the repositories it
should reach (all repositories, or the specific ones the estate declares).
Changing permissions later makes GitHub ask the owner to accept the new set.
