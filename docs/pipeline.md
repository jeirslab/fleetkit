# From a tenant pull request to a deploy

The kit ships reusable GitHub Actions workflows (`.github/workflows/`) that an
estate calls. The kit holds the logic; the estate holds a thin caller per
workflow. Nothing in a kit workflow embeds a secret, a runner label or an
estate name: all of it arrives as an input or a secret from the caller.

## The flow

1. **Tenant repository signals.** A pull request in a tenant repository runs
   `tenant-signal`, which tells the lab repository which tenant pull request
   and commit to check. The signal carries only identifiers (repository,
   pull request number, commit); the lab side never trusts any other value in
   it, least of all an author.
2. **Checks, on a checks runner with no deploy credentials.** The lab
   repository's callers run `check-flake`, `nixos-build` and `tofu-plan`. They
   run on runners selected by the caller (`runs-on`, a JSON list of labels).
   The workspace is the lab repository, because the command that does the
   checking is the lab's own tool; the tenant repository is checked out beside
   it, at the pull request's commit, as an input the command evaluates (see
   [What the command runs in](#what-the-command-runs-in)). Each workflow
   reports a commit status on the tenant commit it was asked about, using a
   token the caller passes, so the result appears on the tenant pull request.
   The checks runner evaluates tenant code from a pull request nobody has
   reviewed yet. It must therefore hold no deploy credentials and no decrypt
   keys. The status token is not in the job that runs the command at all,
   except as the read credential of the tenant checkout: the statuses are
   reported by separate jobs on another runner (see
   [The job split](#the-job-split-where-the-token-is)).
3. **Merge.** A person merges the tenant pull request.
4. **Apply and deploy, on the deploy runner, gated by author.** After the
   merge, `tofu-apply` and `deploy` run on the deploy runner. Both start with
   the `author-gate` action, which reads the pull request's author, merger
   and merge commit from the GitHub API. It compares author and merger with
   the caller's ungated list, and the commit it was asked to run with the
   merge commit. If either login is not on the list, or the commit is not that
   pull request's merge commit, the gate job reports a success status
   described as GATED and nothing runs: the job with the checkouts and the
   command is skipped. A person then runs the step by hand. When the gate is open, the command runs in the
   lab repository with the tenant repository at the merge commit beside it,
   as in the checks. `check-flake`, `nixos-build` and `tofu-plan`
   never deploy and do not use the gate.

```
tenant PR --> tenant-signal --> lab callers --> check-flake / nixos-build / tofu-plan
                                                  (checks runner: lab workspace, tenant commit
                                                   beside it, statuses back to the PR)
merge --> author-gate --> tofu-apply, deploy   (deploy runner: lab workspace, merge commit beside it)
```

The gate and every status are posted from the report runner, not from the
checks or deploy runner.

## What each workflow takes

All are `on: workflow_call`. Every input and secret is declared; none is read
from the environment implicitly. Input names use underscores. Every workflow
takes a `runs_on` input (a JSON list of runner labels, for example
`["self-hosted","nix"]`, read with `fromJSON`) and a secret `token`. The five
that run a command also take the optional `report_runs_on`, a JSON list in the
same form, default `["ubuntu-latest"]`: the runner of the jobs that hold the
token. What the
token is for differs: in `tenant-signal` it only sends the dispatch to the lab
repository, and that workflow reports no status; in the other five it reports
the commit status and reads the repository under check (and, in `tofu-apply`
and `deploy`, its pull request).

| Workflow | Where it runs | Inputs | Secret |
| -------- | ------------- | ------ | ------ |
| `tenant-signal` | tenant repository | `lab_repository`, `tenant_repository`, `pull_request`, `sha`, `runs_on`; optional `event_type` (default `tenant-signal`) | `token`: may create a `repository_dispatch` on the lab repository |
| `check-flake` | checks runner | `runs_on`, `repository`, `sha`, `command`; optional `source_path` (default `tenant`) and `report_runs_on` (default `["ubuntu-latest"]`) | `token`: reads `repository`, writes commit statuses on it |
| `nixos-build` | checks runner | same as `check-flake` | same |
| `tofu-plan` | checks runner | same as `check-flake` | same |
| `tofu-apply` | deploy runner | the above plus `pull_request` and `ungated_authors`; optional `base_branch`. `sha` must be the merge commit of `pull_request` | `token`: also reads the pull request |
| `deploy` | deploy runner | same as `tofu-apply` | same |

`repository` and `sha` name the repository and commit under check: the tenant
pull request's head commit for the three checks, the merge commit for
`tofu-apply` and `deploy`. The status is reported there. `command` is what
runs; the kit does not choose it, so the lab's own tool stays in the lab. The
status context is the workflow's name. Read the `workflow_call` block at the
top of each file for the exact descriptions. "Where it runs" in the table is
where the command runs (`runs_on`).

### The job split: where the token is

The command is code the kit does not control, and in the checks it evaluates
a pull request nobody has reviewed. A step that runs such code can write to
`$GITHUB_ENV` or `$GITHUB_PATH` (a `BASH_ENV`, a fake `gh` first on `PATH`) or
leave a process behind, and so reach into every later step of the same job.
A later step holding the status token would hand it over. Each of the five
workflows is therefore three jobs, and no step of the job that runs the
command has the token in its environment:

| Workflow | Job | Runs on | Holds `token` | Does |
| -------- | --- | ------- | ------------- | ---- |
| `check-flake`, `nixos-build`, `tofu-plan` | `pending` | `report_runs_on` | yes | posts the pending status |
| | `run` (needs `pending`) | `runs_on` | only the source checkout's `with: token` | checkouts, then the command |
| | `report` (needs both, always runs) | `report_runs_on` | yes | posts the final status from the result of `run` |
| `tofu-apply`, `deploy` | `gate` | `report_runs_on` | yes | pending status, fetches the author gate at the pinned kit commit, runs it, posts GATED when it says gated |
| | `run` (needs `gate`, only when the gate said ungated) | `runs_on` | only the source checkout's `with: token` | checkouts, then the command |
| | `report` (needs both, always runs) | `report_runs_on` | yes | final status, or the gate error |

The final status follows the result of `run`: `success` when it succeeded,
`failure` when it failed or was cancelled, `error` when it never started
although it should have (the pending status could not be posted). In
`tofu-apply` and `deploy` a gate job that failed gives the `error` status
"author gate could not decide; nothing was run", and a gated run gets no
second status: the gate job already said GATED.

`report_runs_on` is where the token-holding jobs run. They only call the
GitHub API, so a GitHub-hosted runner is the default. It must not be the
runner that executes the command: a job there would put the token back within
reach of whatever the command left behind on that machine.

What is true of the token and the command, precisely:

- The token is not in the environment of any step of the `run` job, and no
  job that has it there runs on the command's runner. Nothing the command
  writes to `$GITHUB_ENV` or `$GITHUB_PATH`, and no process it leaves behind,
  meets a step that holds the token.
- The `run` job does use the token once, when `repository` is not the
  caller's: as the `with: token` of the checkout that reads it, which a
  private tenant repository needs. The checkout action uses it before the
  command starts and does not persist it (`persist-credentials: false`), so
  it is not in `.git/config`, not in a file of the workspace and not in the
  command's environment.
- That use means the secret is still delivered to the runner that executes
  the command, inside the runner's own process. The workflows take one
  secret for both purposes, so it is the same token that writes statuses.
  Scope it to the repository under check and to what these workflows need
  (read contents, write commit statuses; read pull requests for `tofu-apply`
  and `deploy`), and treat the command's runner as able to see a token of
  that scope if the runner itself is compromised. When `repository` is the
  caller's own, the `run` job does not use the token at all.

### What the command runs in

These five workflows are called from the lab repository, and `command` is the
lab's tool (for example `tools/fleet check`). So the workspace is the
caller's repository, not `repository`:

- **The caller is not `repository`** (the lab checking a tenant). The
  workspace is the caller's repository at the commit the calling workflow
  runs at, read with the job's own token. `repository` is checked out at
  `sha` into `source_path` (a directory under the workspace root, `tenant`
  unless the caller says otherwise) with `token`. The `run` job first
  refuses a `source_path` that is empty, `.`, an absolute path or has a `..`
  component, so the checkout cannot land on or outside the workspace root.
- **The caller is `repository`** (the lab checking its own change). The
  workspace is that repository at `sha`. Nothing else is checked out and
  `source_path` is unused.

No checkout leaves a credential in `.git/config`. `token` is not widened to
read anything but `repository`; the caller's repository is read with the
job's own token.

`command` runs with `bash -c` in the workspace root and receives:

| Variable | Value |
| -------- | ----- |
| `FLEET_SOURCE_REPOSITORY` | `repository` (owner/name) |
| `FLEET_SOURCE_SHA` | `sha` |
| `FLEET_SOURCE_PATH` | absolute path of the `source_path` checkout; empty when the caller is `repository` and the workspace itself is at `sha` |

The lab's command uses these to point its tenant flake input at the checked
out commit (for example `--override-input <tenant> "path:$FLEET_SOURCE_PATH"`
when the variable is not empty). The command receives no token and no other
secret from the workflow. The checks need none; `tofu-apply` and `deploy`
take their deploy credentials from the runner host (see below), not from
GitHub.

### The author gate

`.github/actions/author-gate/action.yml` is a composite action used by
`tofu-apply` and `deploy`. It takes `token`, `repository`, `pull_request`,
`sha`, `ungated_authors` (logins separated by commas, spaces or newlines) and
optionally `base_branch`, and outputs `ungated`, `author`, `merged_by`,
`merge_commit`, `base_ref` and `reason`. `ungated` is `true` only when all of
these hold:

- the pull request has been merged, and both its author and its merger are in
  the list;
- `sha` is the pull request's merge commit (`merge_commit_sha`);
- `base_branch`, when given, is the branch it was merged into (`base.ref`).

Author, merger, merge commit and base branch are read from the API with the
given token; nothing about them is taken from the event that started the
workflow or from the tenant signal. If the gate cannot decide, the workflow
reports an error status and runs nothing.

The gate runs in the `gate` job, on the report runner, and hands `ungated`
and `reason` to the other jobs as job outputs. The `run` job starts only when
`needs.gate.outputs.ungated == 'true'`; a gate job that failed or was
cancelled leaves it skipped.

The pull request number and the commit can reach the lab from the tenant
signal, which the tenant repository sends, so neither is trusted on its own.
A number only selects which pull request the API is asked about. The commit
is the one `tofu-apply` and `deploy` check out and act on, and the gate binds it
to that pull request: without the binding, a sender could name any old pull
request merged by an ungated author together with a commit nobody ungated
wrote or merged. With it, the only commit that passes is the one the ungated
merger put on the base branch. This means the caller passes the merge commit
as `sha`, not the pull request's head commit, and the status of `tofu-apply`
and `deploy` is reported on the merge commit. Pass `base_branch` (for example
`main`) so that a pull request merged into some other branch does not count;
without it any base branch does.

The workflows do not name the gate by a branch. They check out the kit at the
commit the workflow file itself was called at (`job.workflow_sha`, from
`job.workflow_repository`) into `.fleetkit` in the `gate` job and use the action from there, so
the caller's SHA pin covers the gate as well. If that commit cannot be
determined (GitHub Enterprise Server does not provide it), the gate job fails
before the gate and nothing runs. The kit checkout uses the job's own token,
which is enough while the kit repository is public.

A gated run reports the state `success`, because a commit status has no
neutral state; the description starts with `GATED`, gives the reason and
says nothing was run.
Do not read a green `tofu-apply` or `deploy` status as "applied" without
reading its description.

## Why callers pin a full commit SHA

A caller names the kit workflow as
`jeirslab/fleetkit/.github/workflows/<name>.yml@<ref>`. The `ref` is a full
40-hex commit SHA, never a branch or a tag. A branch moves under the caller:
anyone who can push to it changes what runs on the estate's runners, with the
estate's secrets, without a change in the estate. A tag can be moved too. A
commit SHA cannot, so a change to what runs is a reviewed change to the
caller. `mkWorkflowCaller` refuses any other `ref` with an evaluation error.

To adopt a newer kit workflow, change the SHA in the estate and review the
change like any other.

## Decrypt keys stay on the runner host

The keys that decrypt the estate's secrets (the sops age keys) live on the
runner host and are not GitHub secrets. A GitHub secret is readable by any
workflow run in that repository and is copied to GitHub; a key on the host is
readable only by jobs that run there. Therefore only the deploy runner, which
the author gate protects, can decrypt deploy credentials. Checks runners
cannot, and a workflow on a hosted runner cannot. The secrets a caller does
pass are the status token and similar narrow tokens, never a decrypt key and
never a credential able to deploy from a checks runner. The workflows pass
none of them on to `command`, and keep the status token out of the job that
runs it (see [The job split](#the-job-split-where-the-token-is)).

## The estate declares its callers as managed files

The estate does not write caller files by hand in the tenant or lab
repository. It declares them as managed files, as described in
[`docs/github.md`](github.md): `repos.<estate>.<key>.files.<path>` renders a
`github_repository_file` resource. The content is the caller's text, rendered
by `lib.mkWorkflowCaller`:

```nix
repos.myestate.lab.files.".github/workflows/check-flake.yml".content =
  fleetkit.lib.mkWorkflowCaller {
    name = "check-flake";
    workflow = "check-flake";
    ref = "0123456789abcdef0123456789abcdef01234567"; # a full commit SHA
    on = { pull_request = { }; };
    "with" = {
      runs_on = builtins.toJSON [ "self-hosted" "checks" ]; # a string holding a JSON list
      repository = "myorg/lab";
      sha = "\${{ github.sha }}";
      command = "tools/fleet check";
    };
    secrets.token = "\${{ secrets.CHECK_TOKEN }}";
    permissions = { contents = "read"; };
  };
```

The arguments are `name`, `workflow`, `ref`, `on`, and optionally `with`,
`secrets` and `permissions`. `with` is a Nix keyword, so the attribute name
must be quoted (`"with" = { ... };`); unquoted it is a syntax error. The kit repository is `jeirslab/fleetkit` unless
overridden. The result is text; the kit renders it and never writes it. As with
every managed file, plan and apply stay in the estate repository, and the
text is escaped so that `${{ ... }}` expressions survive Terraform.

This caller lives in the lab repository and checks the lab's own commit, so
`repository` is the caller's own and the workspace is at `sha`. A caller that
checks a tenant passes the tenant's `repository` and `sha` instead (taken
from the signal) and finds the tenant checkout at `$FLEET_SOURCE_PATH`.

Declaring the callers for a real estate, the runner hosts, and dispatching
anything are outside the kit.
