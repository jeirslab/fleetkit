#!/usr/bin/env python3
"""Check a rendered main.tf.json (lib.mkGithubTerraform on tests/fixtures/gh-mini)
against the pinned integrations/github schema.

  github.py RENDERED.json [--schema FILE] [--full --cases tests/cases-github.json]
  github.py RENDERED.json --personal NAME [--cases tests/cases-github.json]

Fails (exit 1, problems on stderr) when:
  - a resource type is not in the schema, or an argument / nested block name
    (recursively) is not an attribute or block of it (the provider block too);
  - a resource or data name is not a legal Terraform name
    (^[A-Za-z_][A-Za-z0-9_-]*$);
  - the three repositories, the default branch, the team, the memberships and
    the Actions secret are not present;
  - the private repository's environment (organisation on Free) or the
    organisation Actions secret (a selected repository is private) is
    rendered, or is not reported in a locals.fleet_skipped_* with a reason
    naming the plan; any environment deployment policy is rendered; a branch
    of the skipped environment (ENV_BRANCHES) is missing from
    locals.fleet_unrendered;
  - the ruleset is rendered, or is not listed in locals.fleet_skipped_rulesets;
  - a credential or secret value (app_auth fields, token, plaintext_value) is
    not a ${data.sops_file...} reference, or names a data.sops_file that is not
    rendered;
  - a repository lacks archive_on_destroy = true.
With --full (the fixture plus tests/cases-github.json "support" and
"positive", and the isolation module): the public repository's environment
and the organisation secret that selects only it are still rendered; the repo Actions secret, variable,
labels and managed file are rendered with the pinned schema's argument names,
the secret value is a sops reference, the runner appears in locals and not as
a resource, and nothing of the other estate's repository (the case file's
isolation.absent) is rendered.
With --personal NAME (the fixture with the git block of the case file's
"personal" entry NAME, an estate whose git.kind is not "org"): the rendered
environments and locals.fleet_skipped_environments are exactly the entry's
"environments" and "skipped_environments" (the reason names the account, not
an organisation), no organisation resource is rendered, and the skipped
environment's branches stay in locals.fleet_unrendered.
Evaluation only; no network.
"""
import argparse
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from terraform import check_block, walk_strings  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCHEMA = f"{ROOT}/providers/schemas/integrations-github-6.13.0.schema.json"
PROVIDER = "registry.opentofu.org/integrations/github"
META = {"lifecycle", "depends_on", "count", "for_each", "provider", "provisioner"}
SOPS_REF = re.compile(r'^\$\{data\.sops_file\.([A-Za-z0-9_-]+)\.data\["(.+)"\]\}$')
TF_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]*$")
CREDENTIAL_KEYS = {"id", "installation_id", "pem_file", "token", "plaintext_value"}

REPOS = {"app": "app", "site": "site-public", "mirror": "mirror"}
LOGINS = {"alice-example": "admin", "bob-example": "member", "carol-example": "member"}
ENV_BRANCHES = ["main", "release/1.x"]
SECRET_FILE = "secrets/gh.json"


def as_list(v):
    return v if isinstance(v, list) else [v]


def check_schema(doc, schema):
    """Every name is a legal Terraform name; every type and argument is in the schema."""
    problems = []
    schemas = schema["resource_schemas"]
    resources = doc.get("resource", {})

    for kind in ("resource", "data"):
        for rtype, instances in doc.get(kind, {}).items():
            for name in instances:
                if not TF_NAME.match(name):
                    problems.append(f"{kind}.{rtype}.{name}: not a legal Terraform name")

    for rtype, instances in resources.items():
        res = schemas.get(rtype)
        if res is None:
            problems.append(f"resource type '{rtype}' is not in the integrations/github schema")
            continue
        for name, body in instances.items():
            where = f"{rtype}.{name}"
            args = {k: v for k, v in body.items() if k not in META}
            check_block(args, res["block"], where, problems)
            lc = body.get("lifecycle")
            if lc is not None and (not isinstance(lc, dict) or not lc):
                problems.append(f"{where}: empty or malformed lifecycle must be dropped")
    return problems


def check_env_branches(locs):
    """Both branches of app.prod are listed, whether the environment is rendered or skipped."""
    unrendered = locs.get("fleet_unrendered") or []
    return [
        f"locals.fleet_unrendered does not list environment_branch:app.prod:{b}"
        " (an environment's branches stay listed, a skipped environment's too)"
        for b in ENV_BRANCHES
        if f"environment_branch:app.prod:{b}" not in unrendered
    ]


def check(doc, schema, full=None):
    problems = check_schema(doc, schema)
    resources = doc.get("resource", {})
    locs = doc.get("locals", {})

    prov = doc.get("provider", {}).get("github")
    if not isinstance(prov, dict):
        problems.append("provider.github is not rendered")
    else:
        check_block(prov, schema["provider"]["block"], "provider.github", problems)
        if prov.get("owner") != "example-org":
            problems.append(f"provider.github.owner is '{prov.get('owner')}', not 'example-org'")
        auth = as_list(prov.get("app_auth", [None]))[0]
        if not isinstance(auth, dict) or not {"id", "installation_id", "pem_file"} <= set(auth):
            problems.append("provider.github.app_auth must carry id, installation_id and pem_file")

    # Repositories.
    repos = resources.get("github_repository", {})
    for key, name in REPOS.items():
        r = repos.get(key)
        if r is None:
            problems.append(f"missing github_repository.{key}")
            continue
        if r.get("name") != name:
            problems.append(f"github_repository.{key}: name is '{r.get('name')}', not '{name}'")
    for key, r in repos.items():
        if r.get("archive_on_destroy") is not True:
            problems.append(f"github_repository.{key}: archive_on_destroy is not true")
        if r.get("lifecycle", {}).get("prevent_destroy") is not True:
            problems.append(f"github_repository.{key}: lifecycle.prevent_destroy is not true")
    if "app" in repos and repos["app"].get("visibility") != "private":
        problems.append("github_repository.app: visibility is not private")
    if "mirror" in repos and repos["mirror"].get("archived") is not True:
        problems.append("github_repository.mirror: archived is not true")

    if "app" not in resources.get("github_branch_default", {}):
        problems.append("missing github_branch_default.app")
    elif resources["github_branch_default"]["app"].get("branch") != "main":
        problems.append("github_branch_default.app: branch is not 'main'")
    # Free plan: environments cannot exist in a private repository; reported, not rendered.
    # A public repository keeps its environment (the positive case declares site/live).
    envs = resources.get("github_repository_environment", {})
    want_envs = {"site_live"} if full else set()
    if set(envs) != want_envs:
        problems.append(
            f"github_repository_environment is {sorted(envs)}, not {sorted(want_envs)}"
            " (private repositories on the free plan get none, public ones keep theirs)"
        )
    for name, e in envs.items():
        if "deployment_branch_policy" in e:
            problems.append(f"github_repository_environment.{name}: deployment_branch_policy is rendered")
    if full and "environment_branch:site.live:main" not in (locs.get("fleet_unrendered") or []):
        problems.append("locals.fleet_unrendered does not list environment_branch:site.live:main")
    # A skipped environment's branches are still declared and still unrendered.
    problems += check_env_branches(locs)
    if "github_repository_environment_deployment_policy" in resources:
        problems.append("github_repository_environment_deployment_policy is rendered")
    skipped_text = {
        k: json.dumps(v, sort_keys=True) for k, v in locs.items() if k.startswith("fleet_skipped_")
    }
    all_skipped = " ".join(skipped_text.values())
    if not any("prod" in t and "app" in t and "free" in t.lower() for t in skipped_text.values()):
        problems.append("no locals.fleet_skipped_* reports the environment app/prod with the free plan as the reason")

    # Organisation level.
    if len(resources.get("github_organization_settings", {})) != 1:
        problems.append("expected exactly one github_organization_settings")
    memberships = {
        m.get("username"): m.get("role") for m in resources.get("github_membership", {}).values()
    }
    if memberships != LOGINS:
        problems.append(f"github_membership is {memberships}, not {LOGINS}")
    teams = resources.get("github_team", {})
    if "empty" not in teams:
        problems.append("missing github_team.empty")
    if "empty" in resources.get("github_team_members", {}):
        problems.append("github_team_members.empty rendered for a team without members (members needs at least one)")
    # The team key "core.devs" is not a legal name; the address is sanitised.
    if teams.get("core_devs", {}).get("name") != "core.devs":
        problems.append("missing github_team.core_devs with name 'core.devs'")
    for rtype, name in (("github_team_members", "core_devs"), ("github_team_repository", "core_devs_app")):
        r = resources.get(rtype, {}).get(name)
        if r is None:
            problems.append(f"missing {rtype}.{name}")
        elif r.get("team_id") != "${github_team.core_devs.id}":
            problems.append(f"{rtype}.{name}: team_id is not a reference to github_team.core_devs")
    if not resources.get("github_actions_organization_permissions"):
        problems.append("missing github_actions_organization_permissions")
    # An organisation secret that selects only public repositories is still
    # rendered (the positive case declares SITE_TOKEN for gh/site).
    secrets = resources.get("github_actions_organization_secret", {})
    got_secrets = sorted(str(s.get("secret_name")) for s in secrets.values())
    want_secrets = ["SITE_TOKEN"] if full else []
    if got_secrets != want_secrets:
        problems.append(
            f"github_actions_organization_secret names are {got_secrets}, not {want_secrets}"
            " (CI_TOKEN selects a private repository on the free plan)"
        )
    for name, s in secrets.items():
        if s.get("visibility") != "selected":
            problems.append(f"github_actions_organization_secret.{name}: visibility is not selected")
    if not any(
        "CI_TOKEN" in t and "free" in t.lower() for t in skipped_text.values()
    ):
        problems.append("no locals.fleet_skipped_* reports the organisation secret CI_TOKEN with the free plan as the reason")

    # The ruleset needs the team plan; the organisation is on free.
    if resources.get("github_organization_ruleset"):
        problems.append("github_organization_ruleset rendered above the organisation's plan")
    skipped = locs.get("fleet_skipped_rulesets")
    if not isinstance(skipped, (list, dict)) or not any(
        "protect-main" in str(s) for s in (skipped if isinstance(skipped, list) else list(skipped))
    ):
        problems.append("locals.fleet_skipped_rulesets does not list 'protect-main'")

    # No literal credential or secret value.
    sops_files = doc.get("data", {}).get("sops_file", {})
    creds = 0
    for path, val in walk_strings(doc):
        last = path.rsplit(".", 1)[-1]
        if not path.startswith(".resource") and not path.startswith(".provider"):
            continue
        if last in CREDENTIAL_KEYS and (
            path.startswith(".provider.github.app_auth") or last in {"token", "plaintext_value"}
        ):
            creds += 1
            m = SOPS_REF.match(val) if isinstance(val, str) else None
            if not m:
                problems.append(f"{path}: not a ${{data.sops_file...}} reference")
                continue
            name = m.group(1)
            if name not in sops_files:
                problems.append(f"{path}: data.sops_file.{name} is not rendered")
            elif sops_files[name].get("source_file") != SECRET_FILE:
                problems.append(f"data.sops_file.{name}: source_file is not '{SECRET_FILE}'")
        if isinstance(val, str) and "PRIVATE KEY" in val:
            problems.append(f"{path}: contains private key material")
    want = 5 if full else 3
    if creds < want:
        problems.append(f"expected at least {want} credential references, found {creds}")
    if full:
        problems += check_full(doc, full, all_skipped)
    return problems


def only(resources, rtype, **match):
    """The instances of rtype whose arguments equal match."""
    return [
        (n, b)
        for n, b in resources.get(rtype, {}).items()
        if all(b.get(k) == v for k, v in match.items())
    ]


def check_full(doc, cases, all_skipped):
    problems = []
    resources = doc.get("resource", {})
    locs = doc.get("locals", {})

    # Repo Actions secret: schema names, value a sops reference (also swept above).
    hits = only(resources, "github_actions_secret", secret_name="REPO_TOKEN")
    if len(hits) != 1:
        problems.append(f"expected one github_actions_secret REPO_TOKEN, found {len(hits)}")
    for n, b in hits:
        if not b.get("repository"):
            problems.append(f"github_actions_secret.{n}: no repository")
        if not isinstance(b.get("plaintext_value"), str) or not SOPS_REF.match(b["plaintext_value"]):
            problems.append(f"github_actions_secret.{n}: plaintext_value is not a sops reference")
        if "value" in b or "encrypted_value" in b or "value_encrypted" in b:
            problems.append(f"github_actions_secret.{n}: renders a value argument besides plaintext_value")

    # A value that looks like a Terraform reference or directive stays text.
    for n, b in only(resources, "github_actions_variable", variable_name="REF_LOOKALIKE"):
        if b.get("value") != "$${github_repository.app.name} %%{ if true }x%%{ endif }":
            problems.append(f"github_actions_variable.{n}: estate text is not escaped: {b.get('value')!r}")
    if len(only(resources, "github_actions_variable", variable_name="REF_LOOKALIKE")) != 1:
        problems.append("expected one github_actions_variable REF_LOOKALIKE")
    hits = only(resources, "github_actions_variable", variable_name="DEPLOY_TARGET")
    if len(hits) != 1:
        problems.append(f"expected one github_actions_variable DEPLOY_TARGET, found {len(hits)}")
    for n, b in hits:
        if b.get("value") != "staging" or not b.get("repository"):
            problems.append(f"github_actions_variable.{n}: value/repository wrong: {b}")

    labels = resources.get("github_issue_label", {})
    got = {b.get("name"): b for b in labels.values()}
    if set(got) != {"needs-review", "bug"}:
        problems.append(f"github_issue_label names are {sorted(map(str, got))}, not ['bug', 'needs-review']")
    if len(labels) != len(got):
        problems.append("two github_issue_label share a name")
    for name, color in (("needs-review", "d93f0b"), ("bug", "b60205")):
        b = got.get(name)
        if b is None:
            continue
        if b.get("color") != color:
            problems.append(f"github_issue_label '{name}': color is '{b.get('color')}', not '{color}'")
        if not b.get("repository"):
            problems.append(f"github_issue_label '{name}': no repository")
    if got.get("needs-review", {}).get("description") != "Waiting on a reviewer.":
        problems.append("github_issue_label 'needs-review': description is lost")
    if "description" in got.get("bug", {}) and got["bug"]["description"] is not None:
        problems.append("github_issue_label 'bug': a description appeared from nowhere")

    files = only(resources, "github_repository_file", file=".github/workflows/signal.yml")
    if len(files) != 1:
        problems.append(f"expected one github_repository_file .github/workflows/signal.yml, found {len(files)}")
    for n, b in files:
        where = f"github_repository_file.{n}"
        # Declared with a workflow expression, ${{ github.ref }}. Terraform
        # reads JSON strings as templates, so it must be rendered escaped.
        if b.get("content") != "name: signal\non: push\nenv:\n  REF: $${{ github.ref }}\njobs: {}\n":
            problems.append(f"{where}: content is not the declared text with ${{ escaped: {b.get('content')!r}")
        if b.get("branch") not in (None, "main"):  # omitted means the default branch
            problems.append(f"{where}: branch is '{b.get('branch')}', not the default branch 'main'")
        if b.get("commit_message") != "Manage signal workflow":
            problems.append(f"{where}: commit_message is '{b.get('commit_message')}'")
        if b.get("overwrite_on_create") is not True:
            problems.append(f"{where}: overwrite_on_create is not true (overwrite defaults to true)")
        if not b.get("repository"):
            problems.append(f"{where}: no repository")

    # A runner is model data: never a resource, but readable from locals.
    for rtype in resources:
        if "runner" in rtype:
            problems.append(f"{rtype}: a runner must not be a resource (Terraform cannot register one)")
    if "self-hosted" not in json.dumps(locs) or "gh/runner" not in json.dumps(locs):
        problems.append("the runner (labels, guest gh/runner) is not exposed in locals")

    # The skipped resources are still reported (their absence is checked in check()).
    if "free" not in all_skipped.lower():
        problems.append("no locals.fleet_skipped_* gives the plan as the reason")

    # Nothing of the other estate's repository reaches this render.
    text = json.dumps({k: v for k, v in doc.items() if k != "locals"}) + json.dumps(locs)
    for needle in cases["isolation"]["absent"]:
        # "thing" is also a word inside other strings; match it as a whole token.
        if re.search(rf'(?<![A-Za-z0-9_-]){re.escape(needle)}(?![A-Za-z0-9_-])', text):
            problems.append(f"the other estate's '{needle}' reached estate gh's render")
    return problems


def check_personal(doc, schema, case):
    """An estate whose git.kind is not "org": environments follow the plan, nothing organisational."""
    problems = check_schema(doc, schema)
    resources = doc.get("resource", {})
    locs = doc.get("locals", {})

    envs = sorted(resources.get("github_repository_environment", {}))
    if envs != sorted(case["environments"]):
        problems.append(f"github_repository_environment is {envs}, not {sorted(case['environments'])}")
    skipped = locs.get("fleet_skipped_environments")
    if skipped != case["skipped_environments"]:
        problems.append(
            f"locals.fleet_skipped_environments is {skipped}, not {case['skipped_environments']}"
        )
    if "organisation" in json.dumps(skipped):
        problems.append("locals.fleet_skipped_environments calls a personal account an organisation")
    problems += check_env_branches(locs)
    for rtype in resources:
        if "organization" in rtype or rtype in ("github_membership", "github_team", "github_team_members", "github_team_repository"):
            problems.append(f"{rtype}: an organisation resource is rendered for an estate that is not an organisation")
    if set(resources.get("github_repository", {})) != set(REPOS):
        problems.append(f"github_repository is {sorted(resources.get('github_repository', {}))}, not {sorted(REPOS)}")
    token = doc.get("provider", {}).get("github", {}).get("token")
    if not isinstance(token, str) or not SOPS_REF.match(token):
        problems.append("provider.github.token is not a ${data.sops_file...} reference")
    return problems


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("rendered")
    ap.add_argument("--schema", default=SCHEMA)
    ap.add_argument("--full", action="store_true", help="also check the new blocks of the positive case")
    ap.add_argument("--personal", metavar="NAME", help="check the render of the case file's personal entry NAME")
    ap.add_argument("--cases", default=f"{ROOT}/tests/cases-github.json")
    a = ap.parse_args()
    with open(a.schema, encoding="utf-8") as f:
        schema = json.load(f)[PROVIDER]
    with open(a.rendered, encoding="utf-8") as f:
        doc = json.load(f)
    cases = None
    if a.full or a.personal:
        with open(a.cases, encoding="utf-8") as f:
            cases = json.load(f)
    if a.personal:
        case = next((c for c in cases["personal"] if c["name"] == a.personal), None)
        if case is None:
            sys.exit(f"github.py: no personal case '{a.personal}' in {a.cases}")
        problems = check_personal(doc, schema, case)
    else:
        problems = check(doc, schema, cases if a.full else None)
    for p in problems:
        print(f"FAIL {p}", file=sys.stderr)
    sys.exit(1 if problems else 0)


if __name__ == "__main__":
    main()
