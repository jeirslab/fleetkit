# GitHub App bootstrap

`tools/github-app-bootstrap` registers a GitHub App from a manifest file and
stores its credentials in an existing sops file. It never prints the private
key, client secret or webhook secret. The manifest is a JSON file you pass in;
this tool does not define the permission list.

## Step 0: render the manifest

The permission tiers are chosen when the manifest is rendered, not by this
tool. The kit renders it with `lib.mkGithubAppManifest` (added by #56; this
command depends on #56 and does not work on a kit revision without it):

```sh
nix eval --json <kit flake ref>#lib.mkGithubAppManifest --apply 'f: f { org = "<org>"; tiers = [ "pipeline" "terraform-admin" ]; }' > manifest.json
```

`<kit flake ref>` is the kit at a revision that has #56: `github:jeirslab/fleetkit`
once #56 is merged, or `.` in a checkout of it. `<org>` is the organization (or
user) the App is registered under. `tiers` is any non-empty subset of the tiers
in `github/app-manifest.nix`; `terraform-admin` includes `pipeline`. The
function also takes `name` (default `<org>-fleet`); leave `redirectUrl` unset,
this tool sets `redirect_url` itself. The result is the JSON file to pass as
`--manifest`; read its `default_permissions` before you register it.

Any other JSON file with `name`, `url` and `default_permissions` works too.

## Step 1: create the App

```sh
tools/github-app-bootstrap --org ORG --manifest app.json \
    --sops-file secrets/github.yaml --key-prefix github.app
```

(`app.json` is the `manifest.json` of step 0.) Use `--user NAME` instead of `--org` to register on a personal account
(`https://github.com/settings/apps/new`). `--key-prefix` is split on `.` or `/`
into the sops path (`github.app` becomes `["github"]["app"]`). Other options:
`--sops` and `--openssl` (commands), `--port`, `--timeout` (default 3000 s),
`--no-browser` (print the local URL instead of opening it), `--rescue-dir`
(below). It needs `sops` on the PATH and is stdlib Python otherwise.

The sops file must already exist; the tool does not create one. Before it
serves the local page, so before anything reaches GitHub, it checks that the
credentials will be storable:

- the file exists and you can read and write it;
- `sops decrypt FILE` succeeds (the output is discarded). That is the real
  test: it fails on a file that is not sops-encrypted and on one your key is
  not a recipient of, the two cases in which `sops set` would fail later;
- the rescue directory exists and is writable.

If any of these fails the tool exits there with the reason, and no App has
been created. It does not look at `.sops.yaml` or creation rules: create the
file with `sops` yourself so the right rule applies to it.

This follows GitHub's "Registering a GitHub App from a manifest" flow:

1. A page on `127.0.0.1` POSTs the manifest (with `redirect_url` set to the
   local callback) and a random `state` to GitHub's App registration URL. You
   confirm the App, and may rename it, in the browser. The manifest must set
   `name`, `url` and `default_permissions`.
2. GitHub redirects to the callback with `code` and `state`. A wrong `state`
   is rejected.
3. The tool exchanges the code at `POST /app-manifests/{code}/conversions`
   (unauthenticated; the code works once and expires after one hour, so all
   steps must finish within the hour).
4. One `sops set --value-stdin` per key stores the result. Values travel on
   stdin only, never in argv or the environment. Every key is attempted even
   if one fails.
5. It prints the App's slug, id, html_url and the install URL. The App id is
   not a secret.

The person who confirms the page owns the App registration and must be an
owner of the organization.

## If storing fails

GitHub returns the private key, client secret and webhook secret once. If the
tool cannot finish after the code was exchanged (a `sops set` fails, the disk
is full, the key went away mid-run, the response was cut short or lacks the
key), it does not drop them and does not print them. It writes what GitHub
returned to a new file

    <rescue dir>/github-app-rescue-<time>-<random>.decrypted.json

created with mode 0600, prints that path, lists by name which keys were stored
and which were not, and exits non-zero. The rescue directory is `--rescue-dir`,
by default the directory of the sops file (the system temp directory is tried
if that write fails). **That file is plaintext.** The kit's `.gitignore`
ignores `*.decrypted*`; check yours does before leaving it in a repository
even briefly.

Fix what made sops fail, import each key the tool listed as not stored, then
destroy the file. The file is GitHub's response, so its field names differ
from the stored names (`id` is stored as the string `app_id`, `slug` as
`app_slug`, `pem` as `app_pem`; the rest keep their names):

```sh
R=<the rescue file>; F=secrets/github.yaml
jq '.id | tostring'  "$R" | sops set --value-stdin "$F" '["github"]["app"]["app_id"]'
jq .slug             "$R" | sops set --value-stdin "$F" '["github"]["app"]["app_slug"]'
jq .pem              "$R" | sops set --value-stdin "$F" '["github"]["app"]["app_pem"]'
jq .client_id        "$R" | sops set --value-stdin "$F" '["github"]["app"]["client_id"]'
jq .client_secret    "$R" | sops set --value-stdin "$F" '["github"]["app"]["client_secret"]'
jq .webhook_secret   "$R" | sops set --value-stdin "$F" '["github"]["app"]["webhook_secret"]'
shred -u "$R"
```

Do not run the create step again instead: that registers a second App.

If the exchange fails before any response arrives (a timeout, a dropped
connection, an HTTP 5xx), there is nothing to rescue and the tool says the code
may or may not have been spent. Look at the organization's GitHub Apps page:
if the App is there, its credentials can no longer be fetched, so delete it and
start over; if it is not, run the tool again. An HTTP 4xx means GitHub refused
the code and issued nothing.

## What is written where

Under the key prefix in the sops file, all from the one-time exchange (GitHub
will not return the client or webhook secrets again):

| key | content |
| --- | --- |
| `app_id` | numeric App id |
| `app_slug` | URL name of the App |
| `app_pem` | private key |
| `client_id` | OAuth client id |
| `client_secret` | OAuth client secret |
| `webhook_secret` | webhook secret |
| `installation_id` | added by step 3 |

## Step 2: install the App

Registering the App gives it access to nothing. Open the install URL the tool
printed (or Settings, Developer settings, GitHub Apps, the App, Install App),
choose the organization, then select the repositories it should manage (or
all). The installation is what the App's token is scoped to; changing the
manifest's permissions later requires the owner to approve them on the
installation. Do this by hand; it is an organization setting.

## Step 3: record the installation

```sh
tools/github-app-bootstrap --org ORG --sops-file secrets/github.yaml \
    --key-prefix github.app --record-installation
```

This reads `app_id` and `app_pem` from the sops file, signs a short-lived
(under 10 minutes) RS256 JWT with `openssl dgst -sha256 -sign` (the key on
stdin), lists `GET /app/installations`, picks the installation whose account is
the org (or user), and writes `installation_id` under the prefix. It fails if
the App is not installed there, if `openssl` is not on the PATH (checked
before anything is read), or if the sops file does not pass the same
`sops decrypt` check as step 1.

## Retiring an old App

Only after the new App is confirmed working (a `tofu plan` using its
credentials succeeds and CI uses it):

1. Uninstall the old App from the organization (Settings, Installed GitHub
   Apps, Configure, Uninstall).
2. Delete its registration (Developer settings, GitHub Apps, Advanced,
   Delete GitHub App).
3. Remove its keys from the sops file (`sops unset`, or edit with `sops`), and
   any configuration that still references them.
