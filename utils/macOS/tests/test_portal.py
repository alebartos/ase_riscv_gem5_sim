"""Exercise ASE Studio's Portale della Didattica functions against fake_portal.py.

Environment: ASE_STUDIO_DIR (the ase-studio checkout), ASE_STUDIO_PORTAL_API
(the fake portal), ASE_STUDIO_HOST_ROOT and XDG_CONFIG_HOME (scratch folders).
"""
import json
import os
import stat
import sys
import threading
import urllib.request
import zipfile
from pathlib import Path

sys.path.insert(0, os.environ["ASE_STUDIO_DIR"])
import backend  # noqa: E402

# Test the private-file fallback; never touch the real keychain.
backend.portal_keyring = lambda: None

FAKE = os.environ["ASE_STUDIO_PORTAL_API"]
config = Path(os.environ["XDG_CONFIG_HOME"]) / "ase-studio" / "portal.json"


def expect_fail(callable_, status):
    try:
        callable_()
    except ValueError as error:
        message, got = error.args[0]
        assert got == status, (message, got)
        return message
    raise AssertionError("expected failure")


def fake_state():
    with urllib.request.urlopen(FAKE + "/_state") as response:
        return json.load(response)


# Archive inside the submission directory.
folder = backend.active_submission_directory() / "lab_3"
folder.mkdir(parents=True, exist_ok=True)
archive = folder / "YOUR_NAME_lab_3.zip"
with zipfile.ZipFile(archive, "w") as bundle:
    bundle.writestr("main.s", "nop\n" * 5000)

assert backend.portal_build_number("1.2.4") == "010204"
assert backend.portal_status()["loggedIn"] is False
expect_fail(lambda: backend.portal_courses(), 401)
print("wrong password:", expect_fail(lambda: backend.portal_basic_login("s123456", "nope"), 400))
assert backend.portal_status()["loggedIn"] is False

# Session kept in memory only.
status = backend.portal_basic_login("s123456", "secret", False)
assert status["loggedIn"] and not status["remembered"] and not config.exists(), status

courses = backend.portal_courses()["courses"]
print("courses:", [(c["id"], c["shortcode"]) for c in courses])
assert [c["id"] for c in courses] == [304038, 304039, 306839]

# Path validation.
expect_fail(lambda: backend.portal_upload("/etc/passwd"), 400)

first = backend.portal_upload(str(archive))
assert first["ok"] and first["verified"], first
second = backend.portal_upload(str(archive))
assert second == {"ok": False, "conflict": True, "existing": [first["assignment"]]}, second
kept = backend.portal_upload(str(archive), replace=False)
assert kept["verified"] and kept["withdrawn"] == [], kept
replaced = backend.portal_upload(str(archive), replace=True)
assert sorted(replaced["withdrawn"]) == sorted([first["assignment"]["id"], kept["assignment"]["id"]]), replaced
state = fake_state()
active = [a for a in state["assignments"]["306839"] if not a["deletedAt"]]
assert [a["id"] for a in active] == [replaced["assignment"]["id"]], active
upload = state["uploads"][0]
print("upload as received:", upload)
assert upload == {"description": archive.stem, "filename": archive.name,
                  "type": "application/zip", "bytes": archive.stat().st_size, "zip": True}
login = state["logins"][-1]
print("login body:", login)
assert login["client"] == {"name": "ASE Studio", "appVersion": backend.STUDIO_VERSION,
                           "buildNumber": backend.portal_build_number()}

# Remembered session: file permissions and reload from disk.
backend.portal_basic_login("s123456", "secret", True)
assert config.exists()
assert stat.S_IMODE(config.stat().st_mode) == 0o600
assert stat.S_IMODE(config.parent.stat().st_mode) == 0o700
stored = json.loads(config.read_text())
assert set(stored) == {"username", "token", "clientId"} and "secret" not in config.read_text()
assert fake_state()["logins"][-1]["client"]["id"] == "client-1"
backend._PORTAL_SESSION = None
backend._PORTAL_SESSION_LOADED = False
assert backend.portal_status()["remembered"] is True

# Expired token: 401, session and file cleared.
backend._PORTAL_SESSION["token"] = "expired"
backend._PORTAL_SESSION_LOADED = True
print("expired:", expect_fail(lambda: backend.portal_courses(), 401))
assert backend.portal_status()["loggedIn"] is False and not config.exists()

# Logout.
backend.portal_basic_login("s123456", "secret", True)
assert backend.portal_logout()["loggedIn"] is False and not config.exists()

# SSO: unavailable without an opener, then a simulated native window.
expect_fail(lambda: backend.portal_sso_start(), 400)
opened = []
backend.PORTAL_SSO_OPENER = lambda uid, url: opened.append((uid, url))
uid = backend.portal_sso_start(False)["uid"]
assert opened[-1][1] == f"{backend.PORTAL_SSO_START}?platform=linux&uid={uid}"
assert backend.portal_sso_state(uid)["state"] == "pending"
thread = threading.Thread(target=backend.portal_sso_callback,
                          args=(f"polito://students/login?uid={uid}&key=bad-key",))
thread.start(); thread.join()
bad = backend.portal_sso_state(uid)
assert bad["state"] == "error" and "Invalid SSO key" in bad["error"], bad
uid = backend.portal_sso_start(False)["uid"]
backend.portal_sso_callback(f"polito://students/login?uid={uid}&key=good-key")
done = backend.portal_sso_state(uid)
assert done["state"] == "done" and done["loggedIn"], done
assert fake_state()["logins"][-1]["loginType"] == "sso"
uid = backend.portal_sso_start(False)["uid"]
backend.portal_sso_cancel(uid)
backend.portal_sso_callback(f"polito://students/login?uid={uid}&key=good-key")
assert backend.portal_sso_state(uid)["state"] == "cancelled"
expect_fail(lambda: backend.portal_sso_state("nope"), 404)

# Unreachable portal.
backend.PORTAL_API = "http://127.0.0.1:9"
print("unreachable:", expect_fail(lambda: backend.portal_courses(), 502))
print("session, upload and SSO OK")

# Elaborati list, withdraw/restore, preview.
backend.PORTAL_API = FAKE
backend.portal_basic_login("s123456", "secret", False)
listed = backend.portal_assignments()["assignments"]
assert len(listed) == 3 and sum(1 for a in listed if a["deletedAt"]) == 2, listed
target = next(a for a in listed if not a["deletedAt"])
after = backend.portal_set_withdrawn(target["id"], True)["assignments"]
assert all(a["deletedAt"] for a in after)
after = backend.portal_set_withdrawn(target["id"], False)["assignments"]
assert not next(a for a in after if a["id"] == target["id"])["deletedAt"]
preview = backend.portal_upload_preview(str(archive))
assert preview["filename"] == archive.name and preview["entries"] == [{"path": "main.s", "size": 20000}]
assert [e["id"] for e in preview["existing"]] == [target["id"]]
expect_fail(lambda: backend.portal_set_withdrawn("1", True), 400)
assert preview["course"]["id"] == 306839 and preview["course"]["shortcode"] == "02GOLYG"
# A student without the ASE course gets a clear error.
real_courses = backend.portal_courses
# English edition, with a different degree-programme suffix.
backend.portal_courses = lambda: {"ok": True, "courses": [
    {"id": 1, "name": "Cloud", "shortcode": "01HFPOV", "year": 2027},
    {"id": 777, "name": "Computer architectures", "shortcode": "02LSEOV", "year": 2027}]}
backend._PORTAL_COURSE.clear()
assert backend.portal_course()["id"] == 777
# Italian edition of another programme; the newest year wins.
backend.portal_courses = lambda: {"ok": True, "courses": [
    {"id": 5, "name": "Architetture dei sistemi di elaborazione", "shortcode": "02GOLOV", "year": 2026},
    {"id": 6, "name": "Architetture dei sistemi di elaborazione", "shortcode": "02GOLOV", "year": 2027}]}
backend._PORTAL_COURSE.clear()
assert backend.portal_course()["id"] == 6
backend.portal_courses = lambda: {"ok": True, "courses": [
    {"id": 1, "name": "Cloud", "shortcode": "01HFPOV", "year": 2027}]}
backend._PORTAL_COURSE.clear()
print("not enrolled:", expect_fail(lambda: backend.portal_assignments(), 400))
backend.portal_courses = real_courses
print("Elaborati and course selection OK")
