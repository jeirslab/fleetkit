"""The Ansible add-on's runs (ansible.py, infra.apply, pipeline.run) and the
store paths a stack needs (render: `realise`), with the fake engine: a deploy
reports, per playbook resource, whether it ran and whether its recap says it
changed something; a deploy that does not touch it reports it did not run;
the output itself never reaches an event. Offline. The provider itself, run
by a real engine, is in docs/addons.md ("Verified")."""
import json

import fakes
from fleetkit_cli import ansible, pipeline
from fleetkit_cli.events import Emitter
from fleetkit_cli.pipeline import DeployRequest

PLAY = ansible.TYPE

CHANGED = """
PLAY [all] *********************************************************************

TASK [vyos.vyos.vyos_system] ***************************************************
changed: [router]

PLAY RECAP *********************************************************************
router                     : ok=2    changed=1    unreachable=0    failed=0    skipped=0    rescued=0    ignored=0
"""
UNCHANGED = CHANGED.replace("changed: [router]", "ok: [router]").replace("changed=1", "changed=0")


def deploy(estate, events=None, **req):
    ev = Emitter((events if events is not None else []).append)
    return pipeline.run(estate.s, DeployRequest(estate="mini", nixos=False, **req), ev)


def program(playbook="/nix/store/00000000000000000000000000000000-vyos/site.yml"):
    return fakes.program(vyos={"type": PLAY, "properties": {
        "name": "router", "playbook": playbook, "replayable": True,
        "extraVars": {"ansible_host": "192.0.2.95", "ansible_user": "vyos",
                      "vyos_password_file": "/run/secrets/vyos-admin"}},
        "options": {"replaceOnChanges": ["*"]}})


def test_recap_reads_the_last_play_recap_per_host():
    two = CHANGED + "\nPLAY RECAP ***\nrouter : ok=1 changed=0 unreachable=1 failed=0\nother : ok=3 changed=2\n"
    assert ansible.recap(CHANGED) == {"router": {"ok": 2, "changed": 1, "unreachable": 0, "failed": 0,
                                                 "skipped": 0, "rescued": 0, "ignored": 0}}
    assert ansible.recap(two) == {"router": {"ok": 1, "changed": 0, "unreachable": 1, "failed": 0},
                                  "other": {"ok": 3, "changed": 2}}
    assert ansible.recap("") == {} and ansible.recap("PLAY [all] ***\nfatal: [router]: UNREACHABLE!") == {}


def test_a_deploy_reports_a_playbook_that_ran_and_changed(estate):
    w = estate.stack(program())
    w.outputs["vyos"] = {"ansiblePlaybookStdout": CHANGED + "a secret a task printed"}
    events = []
    result = deploy(estate, events)
    assert result["ansible"] == {"mini-guests": [{"key": "vyos", "ran": True, "changed": True, "recap": {
        "router": {"ok": 2, "changed": 1, "unreachable": 0, "failed": 0, "skipped": 0, "rescued": 0,
                   "ignored": 0}}}]}
    said = [e for e in events if e["kind"] == "ansible"]
    assert [(e["key"], e["ran"], e["changed"]) for e in said] == [("vyos", True, True)]
    assert "a secret a task printed" not in json.dumps(events)


def test_an_unchanged_recap_and_a_playbook_that_did_not_run(estate):
    w = estate.stack(program())
    w.outputs["vyos"] = {"ansiblePlaybookStdout": UNCHANGED}
    first = deploy(estate)
    assert first["ansible"]["mini-guests"][0]["ran"] and not first["ansible"]["mini-guests"][0]["changed"]
    # Nothing declared changed: the engine leaves the resource as it is, and
    # the playbook does not run.
    again = deploy(estate)
    assert again["ansible"] == {"mini-guests": [{"key": "vyos", "ran": False, "changed": False, "recap": {}}]}


def test_a_replaced_playbook_ran(estate):
    w = estate.stack(program())
    deploy(estate)
    w.force["vyos"] = {"steps": ["create-replacement", "replace", "delete-replaced"], "diff": ["playbook"],
                       "keys": ["playbook"]}
    w.outputs["vyos"] = {"ansiblePlaybookStdout": CHANGED}
    result = deploy(estate)
    assert [(r["key"], r["ran"], r["changed"]) for r in result["ansible"]["mini-guests"]] == [("vyos", True, True)]


def test_a_stack_without_playbooks_reports_none(estate):
    estate.stack(fakes.program(apps={"type": fakes.POOL, "properties": {"poolId": "apps"}}))
    assert "ansible" not in deploy(estate)


def test_what_a_stack_needs_is_realised_before_anything_runs(estate, monkeypatch):
    estate.stack(program())
    need = ["/nix/store/00000000000000000000000000000000-vyos.drv^out", "/nix/store/11111111111111111111111111111111-tune.yml"]
    estate.stacks["mini-guests"]["realise"] = need
    built = []
    real_root = pipeline.render._root

    def root(s, link, target):
        built.append((link.name, target))
        return real_root(s, link, target)
    monkeypatch.setattr(pipeline.render, "_root", root)
    events = []
    deploy(estate, events, preview=True)
    assert built[1:] == [("needs-0", need[0]), ("needs-1", need[1])]
    done = [e for e in events if e["stage"] == "render" and e["kind"] == "done"]
    assert done and done[0]["realised"] == need
