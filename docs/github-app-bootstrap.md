# GitHub App bootstrap

`tools/github-app-bootstrap` registers a GitHub App from a manifest file and
stores its credentials in an existing sops file. It never prints the private
key, client secret or webhook secret. The manifest is a JSON file you pass in;
this tool does not define the permission list. Because the manifest is a plain
file, there is no `--tiers` option: pass the manifest for the tier you want.

## Step 1: create the App

```sh
tools/github-app-bootstrap --org ORG --manifest app.json \
    --sops-file secrets/github.yaml --key-prefix github.app
```

Use `--user NAME` instead of `--org` to register on a personal account
(`https://github.com/settings/apps/new`). `--key-prefix` is split on `.` or `/`
into the sops path (`github.app` becomes `["github"]["app"]`). Other options:
`--sops` and `--openssl` (commands), `--port`, `--timeout` (default 3000 s),
`--no-browser` (print the local URL instead of opening it).

The sops file must already exist and match a creation rule in `.sops.yaml`, so
your key can write it; the tool refuses otherwise instead of letting sops
invent a file. Stdlib Python otherwise.

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
   stdin only, never in argv, the environment, or a temp file.
5. It prints the App's slug, id, html_url and the install URL. The App id is
   not a secret.

The person who confirms the page owns the App registration and must be an
owner of the organization.

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
the App is not installed there.

## Retiring an old App

Only after the new App is confirmed working (a `tofu plan` using its
credentials succeeds and CI uses it):

1. Uninstall the old App from the organization (Settings, Installed GitHub
   Apps, Configure, Uninstall).
2. Delete its registration (Developer settings, GitHub Apps, Advanced,
   Delete GitHub App).
3. Remove its keys from the sops file (`sops unset`, or edit with `sops`), and
   any configuration that still references them.
