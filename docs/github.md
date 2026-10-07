# Rendering GitHub from the model (`lib.mkGithubTerraform`)

`lib.mkGithubTerraform { fleet, estate }` turns the evaluated model into an
attrset that `builtins.toJSON` writes as a valid `main.tf.json` for the
`integrations/github` provider (pinned at 6.13.0, the version the schema in
`providers/schemas/` is pinned to). It is a pure evaluation: the kit renders
text and never runs tofu or terraform. Plan, import and apply stay in the
estate repo.

## What is rendered, and from where

| Output | Source in the model |
| ------ | ------------------- |
| `terraform.required_providers` | `integrations/github` 6.13.0, plus `carlpett/sops` when a secret is read |
| `provider.github` | `owner` is `git.org`; authentication from `git.auth`. `kind = "app"` renders an `app_auth` block (`id`, `installation_id`, `pem_file`), `kind = "token"` renders `token`. Every value is a `${data.sops_file...}` reference resolved from the estate's secret refs, with the same ref form and nested-key rule as `mkTerraform`; never a literal credential |
| `resource.github_repository.<key>` | each `fleet.repos.<estate>.<key>`: `name` (the repo's `name`, else its key), `visibility`, `description`, `has_issues` / `has_wiki` / `has_projects` from `features`, merge settings from `merge`, `archived`. `archive_on_destroy` (true unless the repository sets `archiveOnDestroy = false`) and `lifecycle.prevent_destroy = true` |
| `resource.github_branch_default.<key>` | repositories that set `defaultBranch` |
| `resource.github_repository_environment.<key>_<environment>` | each environment of a repository (its `branches` are not rendered, see below); not rendered for a private repository when `git.plan` is `free` |
| `resource.github_actions_secret.<key>_<NAME>` | `repos.<estate>.<key>.actions.secrets.<NAME>.sourceRef`; `plaintext_value` is always a `${data.sops_file...}` reference |
| `resource.github_actions_variable.<key>_<NAME>` | `repos.<estate>.<key>.actions.variables.<NAME>` (the value is plain text, not a secret) |
| `resource.github_issue_label.<key>_<name>` | `repos.<estate>.<key>.labels.<name>` (`color`, optional `description`); one resource per label, so labels made by hand are left alone |
| `resource.github_repository_file.<key>_<path>` | `repos.<estate>.<key>.files.<path>` (`content`, optional `branch`, `message`, `overwrite`): `file`, `content`, `overwrite_on_create`, and `branch` / `commit_message` when set (otherwise the default branch and the provider's message). Meant for a workflow file the lab keeps in a tenant repository |
| `github_organization_settings` | `git.organization`, when `git.kind == "org"` |
| `github_membership` | one per member; the role comes from the `admin` / `member` lists, and a principal id is resolved to its `github` login through `fleet.operators.principals` |
| `github_team`, `github_team_members`, `github_team_repository` | `git.teams`; membership is authoritative |
| `github_actions_organization_permissions` | `git.actions` |
| `github_actions_organization_secret` | each `git.actions.secrets` entry; `plaintext_value` is a `${data.sops_file...}` reference, `visibility = "selected"` with `selected_repository_ids` referencing the rendered repositories. Not rendered when `git.plan` is `free` and any selected repository is private |
| `github_organization_ruleset` | each ruleset whose `requiresPlan` is satisfied by `git.plan` (`free` < `team` < `enterprise`) |
| `locals.fleet_skipped_rulesets` | rulesets not rendered because the plan is too low, with the reason |
| `locals.fleet_skipped_environments` | environments not rendered (private repository on a Free organisation), with the reason |
| `locals.fleet_skipped_org_secrets` | organisation secrets not rendered (Free plan, a selected repository is private), with the reason and the repositories |
| `locals.fleet_runners` | the runners declared in `repos.<estate>.<key>.runners` (`repository`, `name`, `labels`, `on`); report data only |
| `locals.fleet_forks` | repositories that are forks |
| `locals.fleet_unrendered` | what the kit cannot render (see below) |

## Resource addresses

The address of a repository is `github_repository.<repo key>`, the key under
`fleet.repos.<estate>`. Renaming a key changes the address and therefore its
state; change `name` instead to rename the repository on GitHub. The other
per-repository resources use the same key.

A Terraform resource name may hold only letters, digits, `_` and `-`, and
must not start with a digit or `-`. Every name the kit generates from a model
key (repo keys, environment names, team keys, principal ids, secret and
ruleset names) is therefore sanitised: any other character becomes `_`, and a
name that would start with a digit or `-` gets a leading `_`. A repo key
`foo.bar` has the address `github_repository.foo_bar`. Two keys that
sanitise to the same name are an evaluation error, not a silent merge.

A team, ruleset or Actions secret may only name repositories of the estate
being rendered; a repository id of another estate is an evaluation error.

## Using it from an estate repo

```nix
let
  tf = fleetkit.lib.mkGithubTerraform {
    inherit fleet;
    estate = "<estate>";
  };
in
pkgs.writeText "main.tf.json" (builtins.toJSON tf)
```

Copy or link the file into the directory where the estate runs tofu, next to
its own backend configuration. Any `lib` argument is supplied by the kit.

## Deliberately not rendered

- Outside collaborators: GitHub holds them per repository and the model does
  not say which, so `git.members.outside` is listed in `locals.fleet_unrendered`.
- Deploy keys: the model names a deploy key and whether it is read-only but
  holds no public key, so no `github_repository_deploy_key` is rendered; each
  is listed in `locals.fleet_unrendered`, never invented.
- Environment branches: an environment's `branches` are not rendered as
  deployment branch policies; each is listed in `locals.fleet_unrendered` as
  `environment_branch:<key>.<environment>:<pattern>`.
- `git.actions.runnerGroups`, `variables` and `workflowPermissions`: listed in
  `locals.fleet_unrendered` when present.
- Rulesets above the organisation's plan: they would fail at apply, so they
  are skipped and listed in `locals.fleet_skipped_rulesets`.
- Environments of a private repository on a Free organisation, and
  organisation secrets that select a private repository on Free: GitHub does
  not provide them there, so they are skipped and reported in
  `locals.fleet_skipped_environments` / `locals.fleet_skipped_org_secrets`.
  Use repository-level secrets and variables instead.
- Runners: Terraform cannot register a self-hosted runner (the runner host
  asks GitHub for its own registration token), so `runners` yields no
  resource, only `locals.fleet_runners` for a host module to read.
- Forks: the provider cannot create a fork relationship. A fork is rendered
  like any repository and noted in `locals.fleet_forks`.
- Repository destruction: removing an entry never deletes a repository
  (`archive_on_destroy`, `prevent_destroy`).

## Adopting an existing organisation

An organisation that already exists is adopted, not recreated. In the estate
repo, write `import` blocks for each rendered address (for example
`import { to = github_repository.<key>; id = "<name>"; }`), then run
`tofu plan`. Adoption is done when the plan is empty: adjust the model, not
the state, until it is. The kit renders text only; it never runs tofu,
imports resources or manages state.

## Check

`tests/github.sh` renders a small fixture (`tests/fixtures/gh-mini/`) and
`tests/github.py` verifies every resource type and argument against the
pinned provider schema, that every resource name is a legal Terraform name,
the expected addresses, the skipped ruleset, that no
credential is a literal and that every repository has `archive_on_destroy`.
It runs under the `fidelity` gate in `tools/gates.sh`.

## Three rules the renderer keeps

- `archive_on_destroy` is true unless a repository sets `archiveOnDestroy = false`: leaving the configuration archives a repository and never deletes it by default.
- `github_team_members` is rendered only for a team that has members; the provider requires at least one.
- `github_actions_organization_permissions` is rendered only when the estate declares `actions.enabledRepositories`. Declaring only secrets or variables must not set an organisation-wide policy.
