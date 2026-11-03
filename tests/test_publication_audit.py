import json
import subprocess
import sys
import zipfile
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/audit_publication.py"


def test_removed_token_is_still_found_in_git_history(tmp_path):
    def git(*args):
        return subprocess.run(["git", "-C", str(tmp_path), *args], check=True, capture_output=True)
    git("init", "-q")
    git("config", "user.name", "Test")
    git("config", "user.email", "test@example.com")
    item = tmp_path / "config.txt"
    old_data = tmp_path / "data" / "accounts.json"
    old_data.parent.mkdir()
    old_data.write_text("{}", "utf-8")
    item.write_text("credential=" + "ghp_" + "a" * 36, "utf-8")
    git("add", ".")
    git("commit", "-qm", "fixture")
    item.write_text("redacted", "utf-8")
    old_data.unlink()
    old_data.parent.rmdir()
    git("add", "-A")
    git("commit", "-qm", "redacted fixture")
    result = subprocess.run([sys.executable, str(SCRIPT), "--repository", str(tmp_path), "--history"], capture_output=True, text=True)
    report = json.loads(result.stdout)
    assert result.returncode == 1
    assert any(i["rule"] == "github-token" and i["path"].startswith("history:") for i in report["issues"])
    assert any(i["rule"] == "private-path" and i["path"].endswith("/data/accounts.json") for i in report["issues"])
    assert "ghp_" + "a" * 36 not in result.stdout


def test_user_data_inside_release_archive_is_rejected(tmp_path):
    archive = tmp_path / "release.zip"
    with zipfile.ZipFile(archive, "w") as z:
        z.writestr("release/data/accounts.json", "{}")
        z.writestr("release/app/_internal/cv2/data/__init__.py", "")
        z.writestr("release/source/sanmou_assistant/main.py", "print('ready')")
    result = subprocess.run([sys.executable, str(SCRIPT), "--archive", str(archive)], capture_output=True, text=True)
    assert result.returncode == 1
    assert json.loads(result.stdout)["issues"] == [{"path": "release/data/accounts.json", "rule": "private-path"}]


def test_private_identity_is_reported_without_disclosing_the_value(tmp_path):
    root = tmp_path / "source"
    root.mkdir()
    (root / "notes.md").write_text("private-fixture-identity", "utf-8")
    markers = tmp_path / "markers.json"
    markers.write_text(json.dumps(["private-fixture-identity"]), "utf-8")
    result = subprocess.run([sys.executable, str(SCRIPT), "--repository", str(root), "--private-markers", str(markers)], capture_output=True, text=True)
    assert result.returncode == 1
    assert "private-fixture-identity" not in result.stdout
    assert json.loads(result.stdout)["issues"] == [{"path": "notes.md", "rule": "private-identity"}]


def test_tls_key_delimiters_are_not_mistaken_for_a_private_key(tmp_path):
    root = tmp_path / "source"
    root.mkdir()
    target = root / "sample.txt"
    begin = "-----BEGIN " + "PRIVATE KEY-----"
    end = "-----END " + "PRIVATE KEY-----"
    target.write_text(begin + "\n" + end, "utf-8")
    command = [sys.executable, str(SCRIPT), "--repository", str(root)]
    assert subprocess.run(command, capture_output=True).returncode == 0
    target.write_text(begin + "\n" + "A" * 80 + "\n" + end, "utf-8")
    result = subprocess.run(command, capture_output=True, text=True)
    assert result.returncode == 1
    assert json.loads(result.stdout)["issues"] == [{"path": "sample.txt", "rule": "private-key"}]
