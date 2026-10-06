#!/usr/bin/env python3
"""Check a rendered main.tf.json (lib.mkGithubTerraform on tests/fixtures/gh-mini)
against the pinned integrations/github schema.

  github.py RENDERED.json [--schema FILE]

Fails (exit 1, problems on stderr) when:
  - a resource type is not in the schema, or an argument / nested block name
    (recursively) is not an attribute or block of it (the provider block too);
  - the three repositories, the default branch, the team, the memberships and
    the Actions secret are not present;
  - the ruleset is rendered, or is not listed in locals.fleet_skipped_rulesets;
  - a credential or secret value (app_auth fields, token, plaintext_value) is
    not a ${data.sops_file...} reference, or names a data.sops_file that is not
    rendered;
  - a repository lacks archive_on_destroy = true.
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
CREDENTIAL_KEYS = {"id", "installation_id", "pem_file", "token", "plaintext_value"}

REPOS = {"app": "app", "site": "site-public", "mirror": "mirror"}
LOGINS = {"alice-example": "admin", "bob-example": "member", "carol-example": "member"}
SECRET_FILE = "secrets/gh.json"


def as_list(v):
    return v if isinstance(v, list) else [v]


def check(doc, schema):
    problems = []
    schemas = schema["resource_schemas"]
    resources = doc.get("resource", {})
    locs = doc.get("locals", {})

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
    if len(resources.get("github_repository_environment", {})) != 1:
        problems.append("expected exactly one github_repository_environment")

    # Organisation level.
    if len(resources.get("github_organization_settings", {})) != 1:
        problems.append("expected exactly one github_organization_settings")
    memberships = {
        m.get("username"): m.get("role") for m in resources.get("github_membership", {}).values()
    }
    if memberships != LOGINS:
        problems.append(f"github_membership is {memberships}, not {LOGINS}")
    teams = resources.get("github_team", {})
    if "devs" not in teams:
        problems.append("missing github_team.devs")
    for rtype in ("github_team_members", "github_team_repository"):
        if not resources.get(rtype):
            problems.append(f"missing {rtype}")
    if not resources.get("github_actions_organization_permissions"):
        problems.append("missing github_actions_organization_permissions")
    secrets = resources.get("github_actions_organization_secret", {})
    if len(secrets) != 1:
        problems.append("expected exactly one github_actions_organization_secret")
    for name, s in secrets.items():
        if s.get("secret_name") != "CI_TOKEN":
            problems.append(f"github_actions_organization_secret.{name}: secret_name is not CI_TOKEN")
        if s.get("visibility") != "selected":
            problems.append(f"github_actions_organization_secret.{name}: visibility is not selected")

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
    if creds < 4:
        problems.append(f"expected at least 4 credential references (3 app_auth + 1 secret), found {creds}")
    return problems


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("rendered")
    ap.add_argument("--schema", default=SCHEMA)
    a = ap.parse_args()
    with open(a.schema, encoding="utf-8") as f:
        schema = json.load(f)[PROVIDER]
    with open(a.rendered, encoding="utf-8") as f:
        doc = json.load(f)
    problems = check(doc, schema)
    for p in problems:
        print(f"FAIL {p}", file=sys.stderr)
    sys.exit(1 if problems else 0)


if __name__ == "__main__":
    main()
