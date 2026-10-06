export const meta = {
  name: 'issue-review',
  description: 'One reviewer checks that an issue branch handles its issue; approves it for converge or sends findings back',
  whenToUse: 'After any execution template has pushed an issue branch. Called by the execution templates; can also be run alone on a branch someone made by hand.',
  phases: [{ title: 'Review', detail: 'one reviewer, re-runs the gates itself' }],
}

// args: { repo, checkout, gates, issue, branch, worktree, breaking? }
const A = args || {}
const RULES = `
RULES (hard):
- Never deploy, never run colmena/tofu/terraform apply, never merge, never push, never use production credentials or read secret files.
- Work only in the worktree ${A.worktree}. Never edit the main checkout ${A.checkout}.
- You did not write this change. Do not trust claims in commit messages; check them.`

const VERDICT = {
  type: 'object',
  properties: {
    approved: { type: 'boolean' },
    gatesLine: { type: 'string', description: 'the JSON line the gates printed when YOU ran them' },
    findings: {
      type: 'array',
      items: {
        type: 'object',
        properties: {
          file: { type: 'string' },
          problem: { type: 'string' },
          fix: { type: 'string' },
        },
        required: ['problem'],
      },
    },
    summary: { type: 'string' },
  },
  required: ['approved', 'findings', 'summary'],
}

phase('Review')
const lenses = [
  `Does the change do what the issue and its approved plan ask? Take each acceptance check from the plan comment and find where the diff satisfies it. An unmet check is a finding. Changes the plan did not ask for are a finding (scope).`,
]
if (A.breaking) {
  lenses.push(
    `This is a BREAKING change. Look only at what breaks for consumers: the tenant repo (XG-Capital-Strategies/deployments reads this model), the old pipeline, locked flake inputs, option names other files use. Every break must be named in the branch (commit message or a doc) together with how to migrate. A break that is not named is a finding.`
  )
}

const verdicts = await parallel(
  lenses.map((lens, i) => () =>
    agent(
      `You review the branch ${A.branch} of ${A.repo} for issue #${A.issue}.
${RULES}

Read first:
- the issue and all its comments: gh issue view ${A.issue} --repo ${A.repo} --comments (the approved plan is the comment that starts with "## Plan").
- the diff: git -C ${A.worktree} diff origin/unstable...HEAD

Then run the gates yourself in the worktree: cd ${A.worktree} && ${A.gates}   (evaluation only; several minutes). A failing gate is a finding.

Your lens: ${lens}

Also always check: no secret material, no key or token, no edit to the tenant boundary (here: lib/default.nix tenantOwned and tenantViolations, and the grant assertions in modules/guests.nix) that the plan did not ask for.

approved = true only if there are no findings. Be specific: file, what is wrong, what would fix it.`,
      { label: `review:#${A.issue}:${i === 0 ? 'handled' : 'breaks'}`, phase: 'Review', model: 'opus', schema: VERDICT }
    )
  )
)

const got = verdicts.filter(Boolean)
const findings = got.flatMap(v => v.findings || [])
return {
  issue: A.issue,
  branch: A.branch,
  approved: got.length === lenses.length && got.every(v => v.approved),
  findings,
  gatesLine: (got[0] && got[0].gatesLine) || '',
  summary: got.map(v => v.summary).join(' / '),
}
