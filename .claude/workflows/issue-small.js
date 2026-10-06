export const meta = {
  name: 'issue-small',
  description: 'Small change for one issue: one worker implements in an outside worktree, one reviewer checks it',
  whenToUse: 'A fix, a config or data edit, a one-file addition: the plan has a single part and nothing breaks for consumers. Two agents (one more round each if the review finds something).',
  phases: [
    { title: 'Implement', detail: 'one worker, own worktree and branch', model: 'sonnet' },
    { title: 'Review', detail: 'issue-review' },
  ],
}

// args: { repo, checkout, gates, issue, workflowsDir? }  (workflowsDir: where issue-review.js is read from; default <checkout>/.claude/workflows)
const A = args || {}
const name = String(A.repo).split('/').pop()
const worktree = `/home/jeirmeister/worktrees/${name}/issue-${A.issue}`
const branch = `issue/${A.issue}`

const RULES = `
RULES (hard):
- Never deploy, never run colmena/tofu/terraform apply, never merge, never open a pull request, never use production credentials or read secret files.
- Work only in the worktree ${worktree}. Never edit the main checkout ${A.checkout}; other issues are being worked there and in sibling worktrees at the same time.
- Evaluation only: never build a system or a whole estate.
- Change what the plan asks and nothing else.`

const WORK = {
  type: 'object',
  properties: {
    ok: { type: 'boolean' },
    gatesLine: { type: 'string' },
    commits: { type: 'array', items: { type: 'string' } },
    summary: { type: 'string' },
    blocker: { type: 'string' },
  },
  required: ['ok', 'summary'],
}

function workPrompt(findings) {
  const fix = findings
    ? `A reviewer found problems. Fix exactly these, then run the gates and push again:\n${JSON.stringify(findings, null, 1)}`
    : `Set up (skip what already exists):
  git -C ${A.checkout} fetch -q origin
  git -C ${A.checkout} worktree add -b ${branch} ${worktree} origin/unstable
  ln -s ${A.checkout}/sources ${worktree}/sources     # baselines the parity gate reads; gitignored
Read the issue and all its comments: gh issue view ${A.issue} --repo ${A.repo} --comments. The approved plan is the comment that starts with "## Plan"; do what it says.`
  return `You implement issue #${A.issue} of ${A.repo} on branch ${branch}.
${RULES}

${fix}

When the change is made: cd ${worktree} && ${A.gates}   (evaluation only; several minutes; it prints one JSON line). Fix until it says "ok":true.
Then commit (message: what changed and why, last line "Refs #${A.issue}") and push: git -C ${worktree} push -u origin ${branch}.
Do not close the issue and do not touch unstable; the converge workflow does that.
If you cannot finish, say why in blocker and set ok false; do not push a broken branch.`
}

phase('Implement')
let work = await agent(workPrompt(null), { label: `work:#${A.issue}`, phase: 'Implement', model: 'sonnet', schema: WORK })
if (!work || !work.ok) {
  return { issue: A.issue, branch, worktree, approved: false, stage: 'implement', blocker: (work && (work.blocker || work.summary)) || 'worker did not return' }
}

phase('Review')
const reviewArgs = { repo: A.repo, checkout: A.checkout, gates: A.gates, issue: A.issue, branch, worktree }
let review = await workflow({ scriptPath: `${A.workflowsDir || A.checkout + "/.claude/workflows"}/issue-review.js` }, reviewArgs)
if (!review.approved) {
  log(`#${A.issue}: review found ${review.findings.length} problem(s); one fix round`)
  work = await agent(workPrompt(review.findings), { label: `fix:#${A.issue}`, phase: 'Implement', model: 'sonnet', schema: WORK })
  if (work && work.ok) review = await workflow({ scriptPath: `${A.workflowsDir || A.checkout + "/.claude/workflows"}/issue-review.js` }, reviewArgs)
}

return {
  issue: A.issue,
  branch,
  worktree,
  approved: !!review.approved,
  stage: 'review',
  findings: review.findings,
  gatesLine: review.gatesLine,
  summary: work ? work.summary : '',
}
