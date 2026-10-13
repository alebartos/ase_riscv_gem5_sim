"""Minimal stand-in for the PoliTO students API (app.didattica.polito.it/api).

Serves the endpoints ASE Studio uses for the Portale della Didattica:
login/logout, courses, and a course's Elaborati (list, multipart upload,
withdraw/restore). GET /api/_state returns what it received.

Usage: fake_portal.py PORT
"""
import email.parser
import email.policy
import json
import re
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

STATE = {"tokens": set(), "assignments": {306839: []}, "next": 1000, "uploads": [],
         "logins": []}
LOCK = threading.Lock()


class Fake(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def reply(self, status, value=None):
        data = json.dumps(value).encode() if value is not None else b""
        self.send_response(status)
        if data:
            self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def body(self):
        return self.rfile.read(int(self.headers.get("Content-Length", "0")))

    def authorized(self):
        token = (self.headers.get("Authorization") or "").removeprefix("Bearer ")
        if token not in STATE["tokens"]:
            self.reply(401, {"code": 401, "message": "Unauthorized"})
            return False
        return True

    def do_POST(self):
        if self.path == "/api/auth/login":
            data = json.loads(self.body())
            STATE["logins"].append(data)
            if data.get("loginType") == "basic" and data.get("password") != "secret":
                return self.reply(401, {"code": 401, "message": "Invalid credentials"})
            if data.get("loginType") == "sso" and data.get("key") != "good-key":
                return self.reply(400, {"code": 400, "message": "Invalid SSO key"})
            token = f"tok{len(STATE['logins'])}"
            STATE["tokens"].add(token)
            return self.reply(200, {"data": {"username": "s123456", "type": "student",
                                             "clientId": "client-1", "token": token}})
        match = re.fullmatch(r"/api/courses/(\d+)/assignments", self.path)
        if match:
            if not self.authorized():
                return
            raw = self.body()
            header = f"Content-Type: {self.headers['Content-Type']}\r\n\r\n".encode()
            message = email.parser.BytesParser(policy=email.policy.default).parsebytes(header + raw)
            fields = {part.get_param("name", header="content-disposition"): part
                      for part in message.iter_parts()}
            upload = fields["file"]
            content = upload.get_payload(decode=True)
            with LOCK:
                STATE["next"] += 1
                STATE["uploads"].append({"description": fields["description"].get_content().strip(),
                                         "filename": upload.get_filename(),
                                         "type": upload.get_content_type(),
                                         "bytes": len(content), "zip": content[:2] == b"PK"})
                STATE["assignments"].setdefault(int(match[1]), []).append({
                    "id": STATE["next"], "description": fields["description"].get_content().strip(),
                    "mimeType": upload.get_content_type(), "filename": upload.get_filename(),
                    "uploadedAt": "2026-10-12T10:00:00+02:00", "deletedAt": None,
                    "sizeInKiloBytes": len(content) // 1024, "url": "https://example.invalid"})
            return self.reply(204)
        self.reply(404, {"code": 404, "message": "Not found"})

    def do_GET(self):
        if self.path == "/api/v2/courses":
            if not self.authorized():
                return
            return self.reply(200, {"data": [
                {"id": 304038, "name": "Programmazione di sistema", "shortcode": "01GRSYG",
                 "year": 2027, "modules": [{"id": 304038, "name": "Programmazione di sistema",
                                            "shortcode": "01GRSYG"},
                                           {"id": 304039, "name": "Programmazione di sistema (mod. 2)",
                                            "shortcode": "01GRSYG"}]},
                {"id": None, "name": "No id", "modules": None},
                {"id": 306839, "name": "Architetture dei sistemi di elaborazione",
                 "shortcode": "02GOLYG", "year": 2027, "modules": []}]})
        match = re.fullmatch(r"/api/courses/(\d+)/assignments", self.path)
        if match:
            if not self.authorized():
                return
            return self.reply(200, {"data": STATE["assignments"].get(int(match[1]), [])})
        if self.path == "/api/_state":
            return self.reply(200, {"uploads": STATE["uploads"],
                                    "assignments": STATE["assignments"],
                                    "logins": [{k: v for k, v in login.items() if k != "password"}
                                               for login in STATE["logins"]]})
        self.reply(404, {"code": 404, "message": "Not found"})

    def do_PATCH(self):
        match = re.fullmatch(r"/api/courses/(\d+)/assignments/(\d+)", self.path)
        if match and self.authorized():
            data = json.loads(self.body())
            for entry in STATE["assignments"].get(int(match[1]), []):
                if entry["id"] == int(match[2]):
                    entry["deletedAt"] = data["deletedAt"]
                    return self.reply(204)
            return self.reply(404, {"code": 404, "message": "No such assignment"})

    def do_DELETE(self):
        if self.path == "/api/auth/logout":
            token = (self.headers.get("Authorization") or "").removeprefix("Bearer ")
            STATE["tokens"].discard(token)
            return self.reply(200, {})
        self.reply(404, {})


if __name__ == "__main__":
    server = ThreadingHTTPServer(("127.0.0.1", int(sys.argv[1])), Fake)
    print("fake portal ready", flush=True)
    server.serve_forever()
