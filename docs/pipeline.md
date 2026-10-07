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
   These runners hold no deploy credentials and no decrypt keys. Each
   workflow reports a commit status on the commit it was asked about, using a
   token the caller passes, so the result appears on the tenant pull request.
3. **Merge.** A person merges the tenant pull request.
4. **Apply and deploy, on the deploy runner, gated by author.** After the
   merge, `tofu-apply` and `deploy` run on the deploy runner. Both start with
   the `author-gate` action, which reads the pull request's author, merger
   and merge commit from the GitHub API. It compares author and merger with
   the caller's ungated list, and the commit it was asked to run with the
   merge commit. If either login is not on the list, or the commit is not that
   pull request's merge commit, the job reports a success status described as
   GATED and runs nothing: the checkout and the command are skipped. A person
   then runs the step by hand. `check-flake`, `nixos-build` and `tofu-plan`
   never deploy and do not use the gate.

```
tenant PR --> tenant-signal --> lab callers --> check-flake / nixos-build / tofu-plan
                                                  (checks runner, statuses back to the PR)
merge --> author-gate --> tofu-apply, deploy   (deploy runner)
```

## What each workflow takes

All are `on: workflow_call`. Every input and secret is declared; none is read
from the environment implicitly. Input names use underscores. Every workflow
takes a `runs_on` input (a JSON list of runner labels, for example
`["self-hosted","nix"]`, read with `fromJSON`) and a secret `token`, which
reports the commit status and, where noted, reads the repository.

| Workflow | Where it runs | Inputs | Secret |
| -------- | ------------- | ------ | ------ |
| `tenant-signal` | tenant repository | `lab_repository`, `tenant_repository`, `pull_request`, `sha`, `runs_on`; optional `event_type` (default `tenant-signal`) | `token`: may create a `repository_dispatch` on the lab repository |
| `check-flake` | checks runner | `runs_on`, `repository`, `sha`, `command` | `token`: reads the repository, writes commit statuses |
| `nixos-build` | checks runner | same as `check-flake` | same |
| `tofu-plan` | checks runner | same as `check-flake` | same |
| `tofu-apply` | deploy runner | the above plus `pull_request` and `ungated_authors`; optional `base_branch`. `sha` must be the merge commit of `pull_request` | `token`: also reads the pull request |
| `deploy` | deploy runner | same as `tofu-apply` | same |

`repository` and `sha` name the repository and commit to check out and to
report the status on. `command` is what runs in the checkout; the kit does not
choose it, so the lab's own tool stays in the lab. The status context is the
workflow's name. Read the `workflow_call` block at the top of each file for
the exact descriptions.

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

The pull request number and the commit can reach the lab from the tenant
signal, which the tenant repository sends, so neither is trusted on its own.
A number only selects which pull request the API is asked about. The commit
is the one `tofu-apply` and `deploy` check out and run, and the gate binds it
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
`job.workflow_repository`) into `.fleetkit` and use the action from there, so
the caller's SHA pin covers the gate as well. If that commit cannot be
determined (GitHub Enterprise Server does not provide it), the job fails
before the gate and runs nothing. The kit checkout uses the job's own token,
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
never a credential able to deploy from a checks runner.

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

Declaring the callers for a real estate, the runner hosts, and dispatching
anything are outside the kit.
