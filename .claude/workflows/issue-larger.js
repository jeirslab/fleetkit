export const meta = {
  name: 'issue-larger',
  description: 'Refactor or new feature for one issue: two or three workers on separate parts, one integrator, one reviewer (two if breaking)',
  whenToUse: 'The plan has separable parts with files that do not overlap (a refactor, a new feature). Pass breaking: true for a breaking change; that adds a reviewer who only looks at what breaks for consumers. Four to six agents.',
  phases: [
    { title: 'Implement', detail: 'one worker per part of the plan, shared worktree', model: 'sonnet' },
    { title: 'Integrate', detail: 'join, run the gates, push the branch' },
    { title: 'Review', detail: 'issue-review' },
  ],
}

// args: { repo, checkout, gates, issue, parts: [{ title, files: [..], task, check }], breaking? }
// `parts` comes from the approved plan: at most three, with files that do not overlap.
const A = args || {}
const name = String(A.repo).split('/').pop()
const worktree = `/home/jeirmeister/worktrees/${name}/issue-${A.issue}`
const branch = `issue/${A.issue}`
const parts = (A.parts || []).slice(0, 3)
if ((A.parts || []).length > 3) log(`#${A.issue}: plan has ${(A.parts || []).length} parts; only the first 3 run. Split the issue.`)
if (parts.length === 0) return { issue: A.issue, approved: false, stage: 'implement', blocker: 'no parts passed; the plan must list them' }

const RULES = `
RULES (hard):
- Never deploy, never run colmena/tofu/terraform apply, never merge, never open a pull request, never use production credentials or read secret files.
- Work only in the worktree ${worktree}. Never edit the main checkout ${A.checkout}.
- Evaluation only: never build a system or a whole estate.
- Change what the plan asks and nothing else.
- The repository owner set this workflow up and approved this issue's plan (the "## Plan" comment). Committing on the issue branch and pushing that branch is the job you were started for; it needs no further go-ahead. What stays off limits is listed above.`

const PART = {
  type: 'object',
  properties: { ok: { type: 'boolean' }, filesChanged: { type: 'array', items: { type: 'string' } }, summary: { type: 'string' }, blocker: { type: 'string' } },
  required: ['ok', 'summary'],
}
const JOINED = {
  type: 'object',
  properties: { ok: { type: 'boolean' }, gatesLine: { type: 'string' }, summary: { type: 'string' }, blocker: { type: 'string' } },
  required: ['ok', 'summary'],
}

// The worktree is created once, before the workers start, so they do not race on it.
phase('Implement')
const setup = await agent(
  `Create the worktree for issue #${A.issue} of ${A.repo} (skip what already exists), then stop:
  git -C ${A.checkout} fetch -q origin
  git -C ${A.checkout} worktree add -b ${branch} ${worktree} origin/unstable
  ln -s ${A.checkout}/sources ${worktree}/sources
Reply with the output of: git -C ${worktree} status -sb`,
  { label: `setup:#${A.issue}`, phase: 'Implement', model: 'sonnet', effort: 'low' }
)
if (!setup) return { issue: A.issue, approved: false, stage: 'implement', blocker: 'could not create the worktree' }

const done = await parallel(
  parts.map(p => () =>
    agent(
      `You implement one part of issue #${A.issue} of ${A.repo}, in the shared worktree ${worktree} (branch ${branch}). Other workers edit other files there right now.
${RULES}
- Edit ONLY these files (create them if the task says so): ${JSON.stringify(p.files)}. Do not run git add, commit or push; the integrator does.
- Do not run the full gates (they stage everything); check your part with: ${p.check || 'nix-instantiate --parse on the files you changed'}

Context: gh issue view ${A.issue} --repo ${A.repo} --comments (the approved plan is the comment that starts with "## Plan").
Your part: ${p.title}
${p.task}`,
      { label: `part:${p.title}`, phase: 'Implement', model: 'sonnet', schema: PART }
    )
  )
)
const failed = parts.filter((p, i) => !done[i] || !done[i].ok)

phase('Integrate')
function joinPrompt(findings) {
  return `You integrate issue #${A.issue} of ${A.repo} in ${worktree} (branch ${branch}).
${RULES}

${findings
    ? `A reviewer found problems. Fix exactly these:\n${JSON.stringify(findings, null, 1)}`
    : `Workers have edited their parts: ${JSON.stringify(done.map((d, i) => ({ part: parts[i].title, result: d })), null, 1)}
${failed.length ? `These parts did not finish; finish them yourself per the plan: ${failed.map(p => p.title).join(', ')}` : ''}
Read the plan (gh issue view ${A.issue} --repo ${A.repo} --comments, the comment that starts with "## Plan") and make the parts fit together.`}

Then: cd ${worktree} && ${A.gates}   (evaluation only; several minutes; one JSON line). Fix until "ok":true.
Commit (what changed and why, last line "Refs #${A.issue}") and push: git -C ${worktree} push -u origin ${branch}.
Do not close the issue and do not touch unstable. If it cannot be made green, set ok false and say why; do not push a broken branch.`
}
let joined = await agent(joinPrompt(null), { label: `integrate:#${A.issue}`, phase: 'Integrate', model: 'opus', schema: JOINED })
if (!joined || !joined.ok) {
  return { issue: A.issue, branch, worktree, approved: false, stage: 'integrate', blocker: (joined && (joined.blocker || joined.summary)) || 'integrator did not return' }
}

phase('Review')
const reviewArgs = { repo: A.repo, checkout: A.checkout, gates: A.gates, issue: A.issue, branch, worktree, breaking: !!A.breaking }
let review = await workflow({ scriptPath: `${A.workflowsDir || A.checkout + "/.claude/workflows"}/issue-review.js` }, reviewArgs)
if (!review.approved) {
  log(`#${A.issue}: review found ${review.findings.length} problem(s); one fix round`)
  joined = await agent(joinPrompt(review.findings), { label: `fix:#${A.issue}`, phase: 'Integrate', model: 'opus', schema: JOINED })
  if (joined && joined.ok) review = await workflow({ scriptPath: `${A.workflowsDir || A.checkout + "/.claude/workflows"}/issue-review.js` }, reviewArgs)
}

return {
  issue: A.issue,
  branch,
  worktree,
  approved: !!review.approved,
  stage: 'review',
  findings: review.findings,
  gatesLine: review.gatesLine,
  summary: joined ? joined.summary : '',
}
