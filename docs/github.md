# The GitHub provider arguments (`lib.internal.github`)

This branch deploys with Pulumi: this stage is what `lib.mkGithubPulumi`
compiles (see `docs/pulumi.md`). `mkGithubTerraform` below is
`lib.internal.github`; nothing runs tofu.

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
| `resource.github_repository.<key>` | each `fleet.repos.<estate>.<key>`: `name` (the repo's `name`, else its key), `visibility`, `description`, `has_issues` / `has_wiki` / `has_projects` from `features`, merge settings from `merge`, `archived`. `archive_on_destroy` (true unless the repository sets `archiveOnDestroy = false`) and `lifecycle.prevent_destroy = true`. Each optional setting the repository sets (see "Optional settings") |
| `resource.github_branch_default.<key>` | repositories that set `defaultBranch` |
| `resource.github_repository_environment.<key>_<environment>` | each environment of a repository (its `branches` are not rendered, see below); not rendered for a private repository when `git.plan` is `free` or unset |
| `resource.github_actions_secret.<key>_<NAME>` | `repos.<estate>.<key>.actions.secrets.<NAME>.sourceRef`; `plaintext_value` is always a `${data.sops_file...}` reference |
| `resource.github_actions_variable.<key>_<NAME>` | `repos.<estate>.<key>.actions.variables.<NAME>` (the value is plain text, not a secret) |
| `resource.github_issue_label.<key>_<name>` | `repos.<estate>.<key>.labels.<name>` (`color`, optional `description`); one resource per label, so labels made by hand are left alone |
| `resource.github_repository_file.<key>_<path>` | `repos.<estate>.<key>.files.<path>` (`content`, optional `branch`, `message`, `overwrite`): `file`, `content`, `overwrite_on_create`, and `branch` / `commit_message` when set (otherwise the default branch and the provider's message). Meant for a workflow file the lab keeps in a tenant repository |
| `github_organization_settings` | `git.organization`, when `git.kind == "org"`: each key is the provider argument in camelCase (see "Optional settings" for the list) |
| `github_membership` | one per member; the role comes from the `admin` / `member` lists, and a principal id is resolved to its `github` login through `fleet.operators.principals` |
| `github_team`, `github_team_members`, `github_team_repository` | `git.teams` (`privacy`, `description`, `members`, `repos`); membership is authoritative |
| `github_actions_organization_permissions` | `git.actions` |
| `github_actions_organization_secret` | each `git.actions.secrets` entry; `plaintext_value` is a `${data.sops_file...}` reference, `visibility = "selected"` with `selected_repository_ids` referencing the rendered repositories. Not rendered at all when `git.plan` is `free` or unset and any selected repository is private |
| `github_organization_ruleset` | each ruleset whose `requiresPlan` is satisfied by `git.plan` (`free` < `pro` < `team` < `enterprise`; a ruleset that required `team` still requires `team`) |
| `locals.fleet_skipped_rulesets` | rulesets not rendered because the plan is too low, with the reason |
| `locals.fleet_skipped_environments` | environments not rendered (private repository, the organisation or the personal account is on Free), with the reason, which names the owner of the plan: `the organisation is on free` when `git.kind == "org"`, `the account is on free` otherwise |
| `locals.fleet_skipped_org_secrets` | organisation secrets not rendered (Free plan, a selected repository is private), with the reason and the repositories |
| `locals.fleet_runners` | the runners declared in `repos.<estate>.<key>.runners` (`repository`, `name`, `labels`, `on`); report data only. `on` is a guest of the repository's own estate (see below) |
| `locals.fleet_forks` | repositories that are forks |
| `locals.fleet_unrendered` | what the kit cannot render (see below) |

## Where the plan is checked

`git.plan` is checked twice. The model refuses a value that is not `free`,
`pro`, `team` or `enterprise`, and `pro` on an organisation, as a failed
assertion of the fleet itself (`modules/loose.nix`), so a tenant check or any
other evaluation of the fleet sees it. The renderer refuses the same two on
every render, for a caller that passes a fleet it did not check.

## Estate text is never a template

Terraform reads every string in `main.tf.json` as a template. Text the estate
supplies (a managed file's path, branch, content and commit message, an
Actions variable's value, a label's name and description, a runner's name
and labels, a repository's `homepageUrl`, `topics`, `template` owner and
repository and `pages` cname, branch and path, a team's `description`, every
string of `git.organization`) is rendered with `${` and
`%{` escaped (`$${`, `%%{`). A workflow file keeps its `${{ ... }}`
expressions, and no value can turn into a reference to another part of the
render, such as a secret. Only the kit's own expressions are templates.

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

## Optional settings

Every option here is unset by default, and an unset option renders nothing:
the argument is left out of the resource, so an estate that sets none of them
renders exactly what it rendered before they existed. That matters at
adoption, because the provider treats an argument the configuration leaves
out as its own default (see "What leaving one out does" below).

### Repository (`fleet.repos.<estate>.<key>`)

| Option | Provider argument | Values |
| ------ | ----------------- | ------ |
| `homepageUrl` | `homepage_url` | text |
| `topics` | `topics` | list of topics |
| `isTemplate` | `is_template` | bool |
| `template = { owner; repository; includeAllBranches ? false; }` | `template { owner, repository, include_all_branches }` | see "A repository created from a template" |
| `pages = { buildType; source = { branch; path; }; cname; }` | `pages { build_type, source { branch, path }, cname }` | `buildType` is `legacy` or `workflow`; every member is optional, but a `source` needs its `branch` |
| `vulnerabilityAlerts` | `vulnerability_alerts` | bool |
| `hasDiscussions` | `has_discussions` | bool |
| `allowAutoMerge` | `allow_auto_merge` | bool |
| `allowUpdateBranch` | `allow_update_branch` | bool |
| `webCommitSignoffRequired` | `web_commit_signoff_required` | bool |
| `squashMergeCommitTitle` | `squash_merge_commit_title` | `PR_TITLE`, `COMMIT_OR_PR_TITLE` |
| `squashMergeCommitMessage` | `squash_merge_commit_message` | `PR_BODY`, `COMMIT_MESSAGES`, `BLANK` |
| `mergeCommitTitle` | `merge_commit_title` | `PR_TITLE`, `MERGE_MESSAGE` |
| `mergeCommitMessage` | `merge_commit_message` | `PR_BODY`, `PR_TITLE`, `BLANK` |

These sit beside `features` and `merge`, not inside them: those two blocks
require all of their members once declared, and a repository must be able to
state one of these settings without restating the rest.

`pages` and `vulnerability_alerts` are the arguments of `github_repository`
itself. Provider 6.13.0 marks both deprecated in favour of the separate
`github_repository_pages` and `github_repository_vulnerability_alerts`
resources; they still work, and a plan prints the deprecation warning. The
kit keeps them on the repository so that adoption needs no further import
ids.

### Team (`git.teams.<team>`)

| Option | Provider argument |
| ------ | ----------------- |
| `description` | `github_team.description` |

### Organisation (`git.organization`)

Each key is the `github_organization_settings` argument in camelCase. Besides
the keys accepted before (`billingEmail`, `defaultRepositoryPermission`,
`hasOrganizationProjects`, `hasRepositoryProjects`, `membersCanCreateRepositories`,
`membersCanCreatePublicRepositories`, `membersCanCreatePrivateRepositories`,
`dependabotAlertsEnabledForNewRepositories`,
`dependencyGraphEnabledForNewRepositories`,
`secretScanningEnabledForNewRepositories`,
`secretScanningPushProtectionEnabledForNewRepositories`,
`webCommitSignoffRequired`):

| Option | Provider argument | Type |
| ------ | ----------------- | ---- |
| `name` | `name` | text |
| `description` | `description` | text |
| `company` | `company` | text |
| `blog` | `blog` | text |
| `email` | `email` | text |
| `location` | `location` | text |
| `twitterUsername` | `twitter_username` | text |
| `membersCanCreateInternalRepositories` | `members_can_create_internal_repositories` | bool |
| `membersCanCreatePages` | `members_can_create_pages` | bool |
| `membersCanCreatePublicPages` | `members_can_create_public_pages` | bool |
| `membersCanCreatePrivatePages` | `members_can_create_private_pages` | bool |
| `membersCanForkPrivateRepositories` | `members_can_fork_private_repositories` | bool |
| `advancedSecurityEnabledForNewRepositories` | `advanced_security_enabled_for_new_repositories` | bool |
| `dependabotSecurityUpdatesEnabledForNewRepositories` | `dependabot_security_updates_enabled_for_new_repositories` | bool |

A key outside the two lists is an evaluation error that names the allowed
keys, and a new key of the wrong type is one too (`expected a string`,
`expected true or false`).

### What leaving one out does

Read from the provider's source at 6.13.0
(`github/resource_github_repository.go`); the pinned schema file carries the
names and the `computed` flag but not the defaults.

- Reset to the provider's default when left out of an adopted repository:
  `homepage_url` (empty), `is_template` (false), `has_discussions` (false),
  `allow_auto_merge` (false), `allow_update_branch` (false),
  `squash_merge_commit_title` (`COMMIT_OR_PR_TITLE`),
  `squash_merge_commit_message` (`COMMIT_MESSAGES`), `merge_commit_title`
  (`MERGE_MESSAGE`), `merge_commit_message` (`PR_TITLE`), and a team's
  `description` (empty). Declare what the live repository has.
- Kept as they are when left out (optional and computed in the schema):
  `topics`, `vulnerability_alerts`, `web_commit_signoff_required`. Declaring
  them is still what makes a later change by hand show up in a plan. An
  empty `topics = [ ]` is likely read as "not set" for the same reason, so it
  does not clear the topics of a repository that has some.
- `pages`: the provider reads a repository's Pages site only when the
  configuration has a `pages` block. Left out, an existing site is neither
  shown nor disabled. Declared and later removed, the site is disabled.
- Every `github_organization_settings` argument but `billing_email` is
  optional and not computed in the pinned schema, so an organisation setting
  left out is planned as the provider's default.

### A repository created from a template

`template` records the template repository a repository was created from.
What provider 6.13.0 does with it (`resource_github_repository.go`):

- **No replacement.** Neither the `template` block nor any of its members is
  `ForceNew` at this version; the resource's only forced replacements are
  changes of `fork`, `source_owner` and `source_repo`. Changing, adding or
  removing `template` plans an update in place. (Older provider releases did
  force a replacement here; this holds for the pinned version only, so
  re-check when the pin moves.)
- **The update does nothing.** The block is read only when the repository is
  created; the update call never sends it.
- **It is read back.** On every refresh the provider sets `template` to the
  live repository's template (`owner`, `repository`), or to nothing.

So for an adopted repository that was created from a template: leaving
`template` out never replaces or changes the repository, but the plan is
never empty either, because each plan proposes to remove the block, the apply
changes nothing, and the next refresh reads it back. The same holds for a
declared owner or repository that is not the live one. Declare the template
the repository really has and the difference is gone. The kit does not hide
this difference with `ignore_changes`: an unset `template` renders nothing,
like every other option here, and a non-empty plan on `template` is the
signal that the declaration is incomplete.

`includeAllBranches` is the one exception. The provider does not read it
back (the API does not report it), so a declared `true` would differ from
state on every plan after the first. When it is `true` the kit therefore
renders `lifecycle.ignore_changes = [ "template[0].include_all_branches" ]`
on that repository (`ignoreChanges: [ template.includeAllBranches ]` in the
Pulumi program). It still takes effect when the repository is created.

### In the Pulumi program

`lib.mkGithubPulumi` compiles the same render, so every argument above is
there under the bridge's name (`homepageUrl`, `isTemplate`,
`vulnerabilityAlerts`, ...). `pages`, its `source` and `template` are
one-item lists in Terraform and plain objects in Pulumi:
`pages: { buildType, cname, source: { branch, path } }`,
`template: { owner, repository, includeAllBranches }`.

## What the schema checks

`modules/repos.nix` refuses these at evaluation, each with a message that
starts with the option path (`fleet.repos.<estate>.<key>...`):

| Option | Rule |
| ------ | ---- |
| `actions.secrets.<NAME>` | the name is letters, digits and `_`, does not start with a digit and does not start with `GITHUB_` (GitHub reserves that prefix) |
| `actions.secrets.<NAME>.sourceRef` | a `sops:` reference that a declared secrets file of the same estate provides; a value that is not a `sops:` reference is refused without being echoed |
| `actions.variables.<NAME>` | the same name rule as a secret |
| `actions.secrets`, `actions.variables` | names are compared without regard to case, as GitHub does: the `GITHUB_` prefix is refused in any case, and two names of one repository that differ only in case are refused |
| `labels.<name>.color` | exactly six hex digits, no leading `#` |
| `files.<path>` | the path is relative to the repository root: not empty, no leading `/`, no `..` component |
| `template` | `owner` and `repository` are both set and not empty |
| `pages.source` | a declared source has a `branch` |
| `pages.buildType` | `legacy` or `workflow` |
| `topics` | each topic is lowercase letters, digits and hyphens, at most 50 characters, and does not start with a hyphen (the provider's own rule) |
| `squashMergeCommitTitle`, `squashMergeCommitMessage`, `mergeCommitTitle`, `mergeCommitMessage` | one of the values GitHub has for that setting |
| `runners.<name>.labels` | at least one label |
| `runners.<name>.on` | a declared guest, and a guest of the same estate as the repository |

`homepageUrl` is not checked for being a URL: GitHub stores whatever text the
field is given, and an adopted repository must be able to declare the value
it has.

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
- Environments of a private repository on a Free organisation or a Free
  personal account, and organisation secrets that select a private repository on Free: GitHub does
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

`git.plan` is `free`, `pro`, `team` or `enterprise`, in that order. **An
unset `git.plan` is treated as `free`.** Any other value is an evaluation
error. `pro` is the plan of a personal account (GitHub Pro); an estate with
`git.kind = "org"` that sets it is an evaluation error, because an
organisation is on `free`, `team` or `enterprise`.

Besides rulesets with a `requiresPlan`, the plan now also decides two more
things, so an organisation on a paid plan must set `git.plan = "team"` or
`"enterprise"` to keep them:

- every environment of a repository that is not `public`. This rule does not
  depend on `git.kind`: it applies to a personal account too (an estate whose
  `git.kind` is not `"org"`). GitHub offers environments to a personal
  account on Free for public repositories only, and for private ones from
  GitHub Pro. A personal account on Pro must therefore set
  `git.plan = "pro"` to keep the environments of its private repositories;
  with the plan unset they are skipped, and the reason reads `the account is
  on free`;
- every organisation secret whose `repos` selects at least one repository
  that is not `public`. The whole secret is dropped, for the public
  repositories in its selection as well, because a secret is one resource
  with one repository list. Narrowing `repos` to public repositories keeps
  it.

Organisation secrets, like every organisation resource, are rendered only
when `git.kind == "org"`, so the second rule never concerns a personal
account.

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

### A tenant: two repositories, a fixed order

The steps above assume one repository holds both the declaration and the
kit lock. A tenant splits them (`docs/tenants.md`): the tenant repository
declares `repos.<tenant>.*` and the estate's `git` block, and the lab
repository evaluates that declaration with the lab's kit lock, renders the
Terraform and holds the state. The tenant repository is a plain source
input and has no kit lock of its own, so the only lock that moves is the
lab's, and the order across the two repositories matters:

1. **Bump the kit in the lab repository first.** The lab change that bumps
   the fleetkit lock also carries step 2 above, the state-removal step: the
   `import` blocks for the dropped addresses are deleted and the addresses
   are taken out of state without destroy (`tofu state rm`, or `removed`
   blocks with `destroy = false`), before that change's first plan. The
   tenant declares nothing new yet.
2. **Only then merge the tenant change** that declares the replacements
   (`repos.<tenant>.<key>.actions.secrets.<NAME>.sourceRef`,
   `actions.variables`, `labels`, `files`, `runners`), and after it update
   the tenant input in the lab (`nix flake update <tenant>`). The
   replacements plan as additions there.

Both wrong orders fail, differently:

- A tenant that declares the new options while the reader is still locked
  to the old kit is an evaluation error: the option
  `fleet.repos.<tenant>.<key>.actions` "does not exist". The lab cannot
  evaluate the tenant at all until its own lock is bumped, which is why the
  tenant change waits for step 1. (A tenant repository that also locks the
  kit for its own checks bumps that lock in the same tenant change.)
- A lab that bumps the kit first, as step 1 requires, drops the tenant's
  private-repository environments and its organisation secrets from the
  rendered Terraform before any replacement is declared. That window is
  expected. It is safe only because the state-removal step is in the same
  lab change: without it the first plan after the bump proposes to destroy
  what the tenant's pipelines still read. Between the two steps the old
  secrets and environments stay on GitHub, unmanaged, and keep working.

## Adopting an existing organisation

An organisation that already exists is adopted, not recreated. In the estate
repo, write `import` blocks for each rendered address (for example
`import { to = github_repository.<key>; id = "<name>"; }`), then run
`tofu plan`. Adoption is done when the plan is empty: adjust the model, not
the state, until it is. An argument the declaration leaves out is planned as
the provider's default, so a live homepage, template flag, Pages site, merge
message setting, team description or organisation profile field must be
declared to survive; "Optional settings" lists them and says which ones the
provider keeps on its own. The kit renders text only; it never runs tofu,
imports resources or manages state.

## Check

`tests/github.sh` renders a small fixture (`tests/fixtures/gh-mini/`) and
`tests/github.py` verifies every resource type and argument against the
pinned provider schema, that every resource name is a legal Terraform name,
the expected addresses, the skipped ruleset, that both branches of the
skipped environment stay in `locals.fleet_unrendered`, that no
credential is a literal and that every repository has `archive_on_destroy`.
`tests/cases-github.json` holds the negative cases: one refused declaration
per rule above, each pinned to the message of the validation that refuses it.
Its positive case sets every optional setting; the render is checked
argument by argument against the pinned schema and the declared values, and
the same model is rendered as a Pulumi program and checked against the pinned
Pulumi schema (`pages` and `template` as objects).
Its `personal` entries render the fixture as a personal account
(`git.kind` not `"org"`), once with `git.plan` unset (the private
repository's environment is skipped, the reason names the account) and once
on `pro` (it is rendered).
It runs under the `fidelity` gate in `tools/gates.sh`.

## Three rules the renderer keeps

- `archive_on_destroy` is true unless a repository sets `archiveOnDestroy = false`: leaving the configuration archives a repository and never deletes it by default.
- `github_team_members` is rendered only for a team that has members; the provider requires at least one.
- `github_actions_organization_permissions` is rendered only when the estate declares `actions.enabledRepositories`. Declaring only secrets or variables must not set an organisation-wide policy.
