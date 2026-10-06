export const meta = {
  name: 'issue-converge',
  description: 'End of an issue session: land every reviewed issue branch on unstable, close the issues, then judge whether to promote unstable to stable',
  whenToUse: 'After one or more execution templates returned approved: true. Pass only approved issues. Two agents.',
  phases: [
    { title: 'Converge', detail: 'merge the branches, gates on the combined result, push unstable, close the issues' },
    { title: 'Promote', detail: 'case-by-case judgement; dispatches the promote Action if warranted' },
  ],
}

// args: { repo, checkout, gates, issues: [{ issue, branch, worktree }] }
const A = args || {}
const name = String(A.repo).split('/').pop()
const worktree = `/home/jeirmeister/worktrees/${name}/converge`
const issues = A.issues || []
if (issues.length === 0) return { landed: [], leftOut: [], promoted: false, reason: 'no approved issues passed' }

const RULES = `
RULES (hard):
- Never deploy, never run colmena/tofu/terraform apply, never use production credentials or read secret files.
- Never force-push. unstable only moves forward.
- Never push to stable and never merge into stable yourself; promotion is the promote Action's job.
- Work only in ${worktree}. Never edit the main checkout ${A.checkout}.
- The repository owner set this workflow up: merging reviewed issue branches into unstable, pushing unstable, closing the issues and dispatching the promote Action are the job you were started for and need no further go-ahead.`

const LANDED = {
  type: 'object',
  properties: {
    landed: { type: 'array', items: { type: 'integer' } },
    leftOut: {
      type: 'array',
      items: { type: 'object', properties: { issue: { type: 'integer' }, why: { type: 'string' } }, required: ['issue', 'why'] },
    },
    gatesLine: { type: 'string' },
    pushed: { type: 'boolean' },
    head: { type: 'string' },
    summary: { type: 'string' },
  },
  required: ['landed', 'leftOut', 'pushed', 'summary'],
}

phase('Converge')
const conv = await agent(
  `You land reviewed issue branches of ${A.repo} on unstable.
${RULES}

Branches (each was reviewed and passed the gates on its own): ${JSON.stringify(issues)}

1. Set up: git -C ${A.checkout} fetch -q origin ; create or reset the converge worktree to origin/unstable:
   git -C ${A.checkout} worktree add -B converge ${worktree} origin/unstable   (if it exists already: git -C ${worktree} fetch -q origin && git -C ${worktree} reset -q --hard origin/unstable, but only if git -C ${worktree} status --porcelain is empty; otherwise stop and report)
   ln -s ${A.checkout}/sources ${worktree}/sources   (skip if present)
2. For each branch in order: git -C ${worktree} merge --no-ff origin/<branch> -m "Merge issue #<n>: <issue title>" with a last line "Closes #<n>".
   On a conflict: resolve it only if both sides' intent is clear from the two issues' plans; otherwise git merge --abort, leave that issue out and record why. One left-out issue must not stop the others.
3. Run the gates on the combined result: cd ${worktree} && ${A.gates}   (evaluation only; one JSON line). If it fails, find which merge broke it (drop the latest merges one at a time with git reset --hard to the commit before them), leave that issue out, and re-run until "ok":true.
4. Push: git -C ${worktree} push origin converge:unstable   (fast-forward only; if rejected, fetch, rebase the merges onto the new origin/unstable by redoing steps 2-3, try once more).
5. For every landed issue: gh issue close <n> --repo ${A.repo} --comment "Landed on unstable in <short sha>. <one line on what changed>."  Then remove its worktree and branches: git -C ${A.checkout} worktree remove <its worktree> ; git -C ${A.checkout} branch -D <branch> ; git -C ${A.checkout} push origin --delete <branch>.
   For every left-out issue: gh issue comment <n> --repo ${A.repo} --body "Not landed: <why>. The branch <branch> is kept." Leave its worktree and branch alone.
6. Do not bring the main checkout ${A.checkout} up to date yourself if it has uncommitted changes; say so in the summary instead. If it is clean and on unstable: git -C ${A.checkout} pull -q --ff-only.`,
  { label: 'converge', phase: 'Converge', model: 'opus', schema: LANDED }
)
if (!conv || !conv.pushed) {
  return { landed: [], leftOut: (conv && conv.leftOut) || [], promoted: false, reason: (conv && conv.summary) || 'converge agent did not return' }
}

const JUDGE = {
  type: 'object',
  properties: { promote: { type: 'boolean' }, reason: { type: 'string' }, dispatched: { type: 'boolean' }, issues: { type: 'array', items: { type: 'integer' } } },
  required: ['promote', 'reason'],
}

phase('Promote')
const judge = await agent(
  `Decide whether ${A.repo} unstable should be promoted to stable now. There is no fixed count or schedule: judge this case.
${RULES}

Look at: git -C ${worktree} log --oneline origin/stable..origin/unstable ; the issues those commits close ; gh issue list --repo ${A.repo} --state open (is something in flight that belongs with this batch, or that would make stable misleading if promoted without it?).

stable is a record of a known-good state. Promote when what is on unstable is a coherent, finished set: the gates are green (they are: ${conv.gatesLine || 'see converge'}), no landed change is half of something whose other half is still open, and there is enough to be worth a record. Do not promote a single trivial change by itself, and do not hold back a finished set waiting for unrelated work.

If you decide to promote: gh workflow run promote.yml --repo ${A.repo} -f reason="<one sentence>"   (the Action opens the unstable->stable pull request as the GitHub App, merges it and notifies the owner). Set dispatched true only if that command succeeded. Do nothing else to stable.
If not: say what would make it ready.`,
  { label: 'promote?', phase: 'Promote', model: 'opus', schema: JUDGE }
)

return {
  landed: conv.landed,
  leftOut: conv.leftOut,
  head: conv.head,
  gatesLine: conv.gatesLine,
  promoted: !!(judge && judge.promote && judge.dispatched),
  reason: judge ? judge.reason : 'promotion was not judged',
  summary: conv.summary,
}
