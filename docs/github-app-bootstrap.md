# GitHub App bootstrap

`tools/github-app-bootstrap` registers a GitHub App on an organization from a
manifest file and writes the App id and private key into that organization's
sops file. It never prints either. The manifest itself is a JSON file you pass
in; this tool does not define the permission list.

```sh
tools/github-app-bootstrap --org ORG --manifest app.json --sops-file secrets/github.yaml
```

Options: `--id-key` / `--key-key` (default `github_app_id`,
`github_app_private_key`), `--sops` (command), `--port`, `--timeout`
(default 3000 s), `--no-browser` (print the local URL instead of opening it).
It needs `sops` and an existing creation rule for the file, so your key can
write it. Stdlib Python otherwise.

## What it does

This follows GitHub's "Registering a GitHub App from a manifest" flow:

1. A page on `127.0.0.1` POSTs the manifest (with `redirect_url` set to the
   local callback) and a random `state` to
   `https://github.com/organizations/ORG/settings/apps/new`. You confirm the
   App, and may rename it, in the browser. The manifest must set `name`,
   `url` and `default_permissions`, and must not enable a webhook.
2. GitHub redirects to the callback with `code` and `state`. A wrong `state`
   is rejected.
3. The tool exchanges the code at `POST /app-manifests/{code}/conversions`
   (unauthenticated; the code works once and expires after one hour, so all
   three steps must finish within the hour).
4. `sops edit` stores the id and the pem. The values pass through the
   environment of the editor helper only, never on a command line or to
   stdout/stderr. The webhook and client secrets in the response are discarded.

The person who confirms the page owns the App registration and must be an
owner of the organization.

## Second step: install the App

Registering the App does not give it access to anything. In the organization:
Settings, Developer settings, GitHub Apps, the new App, Install App, choose
the organization, then select the repositories it should manage (or all). The
installation is what the App's token is scoped to; changing the manifest's
permissions later requires the owner to approve the new permissions on the
installation. Do this by hand; it is an organization setting.
