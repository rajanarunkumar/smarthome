import json
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXT = ROOT / "integrations" / "claude-desktop"


def test_manifest_points_at_the_relay_and_keeps_the_token_secret():
    m = json.loads((EXT / "manifest.json").read_text())
    assert (EXT / m["server"]["entry_point"]).exists()
    assert m["user_config"]["api_token"]["sensitive"] is True
    env = m["server"]["mcp_config"]["env"]
    assert env == {"SMARTHOME_URL": "${user_config.hub_url}", "SMARTHOME_TOKEN": "${user_config.api_token}"}
    assert "ts.net" not in json.dumps({k: v.get("default") for k, v in m["user_config"].items()})


def test_relay_explains_a_missing_address():
    msg = {"jsonrpc": "2.0", "id": 7, "method": "tools/list"}
    out = subprocess.run([sys.executable, str(EXT / "server" / "proxy.py")], input=json.dumps(msg) + "\n",
                         capture_output=True, text=True, env={"SMARTHOME_URL": "", "SMARTHOME_TOKEN": "x"}, timeout=20)
    reply = json.loads(out.stdout)
    assert reply["id"] == 7 and "hub address" in reply["error"]["message"]


def test_build_script_makes_an_archive(tmp_path):
    subprocess.run(["zsh", str(ROOT / "scripts" / "build-claude-extension.sh")], check=True, capture_output=True)
    names = zipfile.ZipFile(ROOT / "dist" / "smarthome-hub.mcpb").namelist()
    assert "manifest.json" in names and "server/proxy.py" in names
