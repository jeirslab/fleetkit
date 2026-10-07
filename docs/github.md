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
| `resource.github_repository_environment.<key>_<environment>` | each environment of a repository (its `branches` are not rendered, see below); not rendered for a private repository when `git.plan` is `free` or unset |
| `resource.github_actions_secret.<key>_<NAME>` | `repos.<estate>.<key>.actions.secrets.<NAME>.sourceRef`; `plaintext_value` is always a `${data.sops_file...}` reference |
| `resource.github_actions_variable.<key>_<NAME>` | `repos.<estate>.<key>.actions.variables.<NAME>` (the value is plain text, not a secret) |
| `resource.github_issue_label.<key>_<name>` | `repos.<estate>.<key>.labels.<name>` (`color`, optional `description`); one resource per label, so labels made by hand are left alone |
| `resource.github_repository_file.<key>_<path>` | `repos.<estate>.<key>.files.<path>` (`content`, optional `branch`, `message`, `overwrite`): `file`, `content`, `overwrite_on_create`, and `branch` / `commit_message` when set (otherwise the default branch and the provider's message). Meant for a workflow file the lab keeps in a tenant repository |
| `github_organization_settings` | `git.organization`, when `git.kind == "org"` |
| `github_membership` | one per member; the role comes from the `admin` / `member` lists, and a principal id is resolved to its `github` login through `fleet.operators.principals` |
| `github_team`, `github_team_members`, `github_team_repository` | `git.teams`; membership is authoritative |
| `github_actions_organization_permissions` | `git.actions` |
| `github_actions_organization_secret` | each `git.actions.secrets` entry; `plaintext_value` is a `${data.sops_file...}` reference, `visibility = "selected"` with `selected_repository_ids` referencing the rendered repositories. Not rendered at all when `git.plan` is `free` or unset and any selected repository is private |
| `github_organization_ruleset` | each ruleset whose `requiresPlan` is satisfied by `git.plan` (`free` < `team` < `enterprise`) |
| `locals.fleet_skipped_rulesets` | rulesets not rendered because the plan is too low, with the reason |
| `locals.fleet_skipped_environments` | environments not rendered (private repository on a Free organisation), with the reason |
| `locals.fleet_skipped_org_secrets` | organisation secrets not rendered (Free plan, a selected repository is private), with the reason and the repositories |
| `locals.fleet_runners` | the runners declared in `repos.<estate>.<key>.runners` (`repository`, `name`, `labels`, `on`); report data only. `on` is a guest of the repository's own estate (see below) |
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

The per-repository resources (Actions secrets, Actions variables, labels,
files) are named `<repo key>_<item>`. That name does not say where the key
ends, so repository `app` with the secret `B_C` and repository `app_B` with
the secret `C` both render `app_B_C`. This is the same evaluation error, and
its message names the repository key and the item separately for each
offender: `repository "app" Actions secret "B_C", repository "app_B" Actions
secret "C" all render the resource name "app_B_C"`.

A team, ruleset or Actions secret may only name repositories of the estate
being rendered; a repository id of another estate is an evaluation error.

## What the schema checks

`modules/repos.nix` refuses these at evaluation, each with a message that
starts with the option path (`fleet.repos.<estate>.<key>...`):

| Option | Rule |
| ------ | ---- |
| `actions.secrets.<NAME>` | the name is letters, digits and `_`, does not start with a digit and does not start with `GITHUB_` (GitHub reserves that prefix) |
| `actions.secrets.<NAME>.sourceRef` | a `sops:` reference that a declared secrets file of the same estate provides; a value that is not a `sops:` reference is refused without being echoed |
| `actions.variables.<NAME>` | the same name rule as a secret |
| `labels.<name>.color` | exactly six hex digits, no leading `#` |
| `files.<path>` | the path is relative to the repository root: not empty, no leading `/`, no `..` component |
| `runners.<name>.labels` | at least one label |
| `runners.<name>.on` | a declared guest, and a guest of the same estate as the repository |

A file, secret, variable, label or runner can only be declared by the estate
that owns the repository: `fleet.repos.<tenant>` is the only part of
`fleet.repos` a tenant source may set (`lib/default.nix`, `tenantViolations`),
so a tenant that sets `fleet.repos.<other estate>.<key>.files.<path>` fails
evaluation with `tenant <tenant> (...) sets fleet.repos outside what a tenant
owns`.

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
  `environment_branch:<key>.<environment>:<pattern>`. This holds for a
  skipped environment too: its branches stay in `locals.fleet_unrendered`
  and the environment itself is in `locals.fleet_skipped_environments`.
- `git.actions.runnerGroups`, `variables` and `workflowPermissions`: listed in
  `locals.fleet_unrendered` when present.
- Rulesets above the organisation's plan: they would fail at apply, so they
  are skipped and listed in `locals.fleet_skipped_rulesets`.
- Environments of a private repository on a Free organisation, and
  organisation secrets that select a private repository on Free: GitHub does
  not provide them there, so they are skipped and reported in
  `locals.fleet_skipped_environments` / `locals.fleet_skipped_org_secrets`.
  Use repository-level secrets and variables instead; see "The plan decides
  what is rendered" and "Migrating an estate that already has them" below.
- Runners: Terraform cannot register a self-hosted runner (the runner host
  asks GitHub for its own registration token), so `runners` yields no
  resource, only `locals.fleet_runners` for a host module to read. A runner's
  `on` must be a declared guest of the same estate as the repository; a
  runner on another estate's guest is an evaluation error (see "What the
  schema checks").
- Forks: the provider cannot create a fork relationship. A fork is rendered
  like any repository and noted in `locals.fleet_forks`.
- Repository destruction: removing an entry never deletes a repository
  (`archive_on_destroy`, `prevent_destroy`).

## The plan decides what is rendered

`git.plan` is `free`, `team` or `enterprise`. **An unset `git.plan` is
treated as `free`.** Besides rulesets with a `requiresPlan`, the plan now
also decides two more things, so an organisation on a paid plan must set
`git.plan = "team"` or `"enterprise"` to keep them:

- every environment of a repository that is not `public`;
- every organisation secret whose `repos` selects at least one repository
  that is not `public`. The whole secret is dropped, for the public
  repositories in its selection as well, because a secret is one resource
  with one repository list. Narrowing `repos` to public repositories keeps
  it.

Each omission is listed with its reason in `locals.fleet_skipped_environments`
or `locals.fleet_skipped_org_secrets`.

## Migrating an estate that already has them

An estate stack that already holds a dropped address in state, or names one
in an `import` block, must be prepared before its first `tofu plan` on a kit
with this change. The addresses are
`github_actions_organization_secret.<NAME>` and
`github_repository_environment.<key>_<environment>`; the ones that apply are
exactly the entries of `locals.fleet_skipped_org_secrets` and
`locals.fleet_skipped_environments`. Left alone, the plan proposes to destroy
them (they are in state and no longer in the configuration), and an `import`
block for one of them fails because its target is gone from the
configuration. Destroying the organisation secret would take away what the
running pipeline reads before its replacement exists. The repository-level
options only exist on the new kit, so both steps go into the change that
bumps the kit, before its first plan:

1. Declare the replacement on each repository that needs the value,
   `repos.<estate>.<key>.actions.secrets.<NAME>.sourceRef` (and
   `actions.variables.<NAME>` for plain values). These plan as additions.
2. Delete the `import` blocks for the dropped addresses, and take the
   addresses out of state without destroying what they manage. Either `tofu state rm <address>` for each, or, kept in
   the estate repo next to the rendered file:

   ```hcl
   removed {
     from = github_actions_organization_secret.<NAME>
     lifecycle {
       destroy = false
     }
   }
   ```

   Either way the old secret or environment stays on GitHub, unmanaged, until
   the pipeline that reads it is retired; delete it by hand then. The plan
   must show no destroy for these addresses before anything is applied.
3. An environment has no equivalent on Free for a private repository. What
   it held moves to the repository: its secrets and variables become
   `actions.secrets` / `actions.variables` of the repository (one set per
   repository, so two environments that held different values under one name
   need two names), and workflows drop their `environment:` key. Protection rules and deployment branches have no replacement
   there; the model's `environments.<env>.branches` may stay declared, it is
   reported in `locals.fleet_unrendered` and renders nothing.

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
`tests/cases-github.json` holds the negative cases: one refused declaration
per rule above, each pinned to the message of the validation that refuses it.
It runs under the `fidelity` gate in `tools/gates.sh`.

## Three rules the renderer keeps

- `archive_on_destroy` is true unless a repository sets `archiveOnDestroy = false`: leaving the configuration archives a repository and never deletes it by default.
- `github_team_members` is rendered only for a team that has members; the provider requires at least one.
- `github_actions_organization_permissions` is rendered only when the estate declares `actions.enabledRepositories`. Declaring only secrets or variables must not set an organisation-wide policy.
