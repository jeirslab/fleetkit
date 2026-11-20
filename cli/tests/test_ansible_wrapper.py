"""ansible_wrapper: the provider's inventory variables reach the play unchanged,
whatever the value (apostrophe, quote, backslash-n, braces, unicode). The
end-to-end test runs the real ansible-playbook when it is on PATH."""
import json
import shutil
import subprocess

import pytest

from fleetkit_cli import ansible_wrapper

VALUE = json.dumps({"description": "it's a \"test\"\nline {{ not_jinja }} é✓", "scope": ["a", "b"]})
INI = f"[vyos]\nvyos ansible_host=127.0.0.1 ansible_connection=local\n\n[vyos:vars]\nkasm_config='{VALUE}'\nplain='x'\n"
PLAY = """- hosts: all
  gather_facts: false
  tasks:
    - copy: {content: "{{ kasm_config }}", dest: "{{ out }}"}
"""


def test_rewrite_moves_variables_to_extra_vars(tmp_path):
    inv = tmp_path / ".inventory-1.ini"
    inv.write_text(INI)
    work = tmp_path / "work"
    work.mkdir()
    args = ansible_wrapper.rewrite(["-i", str(inv), "play.yml"], work)
    assert args[-2] == "-e" and args[-1] == f"@{work / 'vars.json'}"
    assert json.loads((work / "vars.json").read_text()) == {"kasm_config": {"__ansible_unsafe": VALUE}, "plain": {"__ansible_unsafe": "x"}}
    assert "kasm_config" not in (work / ".inventory-1.ini").read_text()


@pytest.mark.skipif(not shutil.which("ansible-playbook"), reason="ansible-playbook is not on PATH")
def test_value_reaches_the_play_unchanged(tmp_path, monkeypatch):
    monkeypatch.setenv("ANSIBLE_LOCAL_TEMP", str(tmp_path / "tmp"))
    monkeypatch.setenv("ANSIBLE_REMOTE_TMP", str(tmp_path / "tmp"))
    inv = tmp_path / ".inventory-2.ini"
    inv.write_text(INI)
    play, out = tmp_path / "play.yml", tmp_path / "out.json"
    play.write_text(PLAY)
    work = tmp_path / "work"
    work.mkdir()
    args = ansible_wrapper.rewrite(["-i", str(inv), "-e", f"out={out}", str(play)], work)
    subprocess.run(["ansible-playbook", *args], check=True, cwd=tmp_path)
    assert out.read_text() == VALUE
