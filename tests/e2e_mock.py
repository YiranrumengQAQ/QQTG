"""End-to-end smoke test with a mock Telegram Bot API.

Run:  python tests/e2e_mock.py  (needs the project's dependencies installed)

It boots the real bridge (``qqtg run``) against the mock, drives the REST API
exactly like the panel does, and asserts that messages flow between two
Telegram groups (A ↔ B) with loop protection / deduplication / media caching.
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx
import uvicorn
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse

ROOT = Path(__file__).resolve().parent.parent
PY = sys.executable
BRIDGE_PORT = 18321
TG_PORT = 18322
TOKEN = "123456789:AAFakeTokenForTestsOnly_ABCDEFGHIJKLMNOPQ"

# the two groups we bridge: A <-> B
GROUP_A = -1001111
GROUP_B = -1002222
TITLES = {GROUP_A: "前端夜雨群", GROUP_B: "玻璃城市闲聊"}

# 1x1 PNG
PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==")



def _parse_multipart(body: bytes, boundary: str):
    """Tiny dependency-free multipart/form-data parser (test mock only)."""
    params: dict[str, str] = {}
    files: dict[str, dict] = {}
    delim = ("--" + boundary).encode()
    for part in body.split(delim):
        part = part.lstrip(b"\r\n")
        if not part or part.startswith(b"--"):
            continue
        headers, sep, content = part.partition(b"\r\n\r\n")
        if not sep:
            continue
        if content.endswith(b"\r\n"):
            content = content[:-2]
        head = headers.decode("utf-8", "replace")
        name = filename = ctype = None
        for line in head.split("\r\n"):
            if line.lower().startswith("content-disposition:"):
                for piece in line.split(";")[1:]:
                    k, _, v = piece.strip().partition("=")
                    v = v.strip().strip('"')
                    if k == "name":
                        name = v
                    elif k == "filename":
                        filename = v
            elif line.lower().startswith("content-type:"):
                ctype = line.split(":", 1)[1].strip()
        if name is None:
            continue
        if filename is not None:
            files[name] = {"filename": filename, "size": len(content), "content_type": ctype or "application/octet-stream"}
        else:
            params[name] = content.decode("utf-8", "replace")
    return params, files


# ----------------------------------------------------------------- mock TG
class MockTelegram:
    def __init__(self) -> None:
        self.app = FastAPI()
        self.updates: asyncio.Queue = asyncio.Queue()
        self.sent: list[dict] = []
        self.next_id = 1000
        self.files: dict[str, bytes] = {"photos/p1.jpg": PNG}
        self.app.post("/bot{token}/{method}")(self.handle)
        self.app.get("/file/bot{token}/{path:path}")(self.file)
        self.app.get("/_media/{name}")(self.media)

    async def media(self, name: str) -> Response:
        return Response(content=PNG, media_type="image/png")

    async def file(self, token: str, path: str) -> Response:
        data = self.files.get(path)
        if data is None:
            return Response(status_code=404)
        return Response(content=data, media_type="application/octet-stream")

    async def handle(self, token: str, method: str, request: Request) -> JSONResponse:
        assert token == TOKEN, token
        ctype = request.headers.get("content-type", "")
        if ctype.startswith("multipart/form-data"):
            boundary = ctype.split("boundary=", 1)[1].strip().strip('"')
            params, files = _parse_multipart(await request.body(), boundary)
        else:
            params = await request.json()
            files = {}
        if method == "getMe":
            return self.ok({"id": 999, "is_bot": True, "first_name": "MockBot", "username": "mock_bot"})
        if method == "deleteWebhook":
            return self.ok(True)
        if method == "getUpdates":
            timeout = float(params.get("timeout", 0) or 0)
            items = []
            try:
                items.append(await asyncio.wait_for(self.updates.get(), timeout=min(timeout, 2.0)))
                while not self.updates.empty():
                    items.append(self.updates.get_nowait())
            except asyncio.TimeoutError:
                pass
            return self.ok(items)
        if method == "getChat":
            cid = int(params["chat_id"])
            return self.ok({"id": cid, "type": "supergroup", "title": TITLES.get(cid, "Mock TG Group"),
                            "permissions": {"can_send_messages": True, "can_send_photos": True, "can_send_videos": True,
                                            "can_send_documents": True, "can_send_audios": True, "can_send_voice_notes": True,
                                            "can_send_other_messages": True}})
        if method == "getChatMember":
            return self.ok({"status": "administrator", "user": {"id": 999}, "can_delete_messages": True})
        if method == "getChatMemberCount":
            return self.ok(42)
        if method == "getFile":
            fid = params["file_id"]
            return self.ok({"file_id": fid, "file_unique_id": "u" + fid, "file_path": "photos/p1.jpg", "file_size": len(PNG)})
        if method == "deleteMessage":
            self.sent.append({"method": method, "params": params})
            return self.ok(True)
        if method.startswith("send"):
            self.next_id += 1
            rec = {"method": method, "params": params, "files": files, "message_id": self.next_id}
            self.sent.append(rec)
            result = {"message_id": self.next_id, "chat": {"id": int(params["chat_id"]), "type": "supergroup"},
                      "date": int(time.time()), "from": {"id": 999, "is_bot": True, "first_name": "MockBot"}}
            if method == "sendPhoto":
                result["photo"] = [{"file_id": "cached-photo-" + str(self.next_id), "file_unique_id": "x", "width": 1, "height": 1}]
            elif method == "sendDocument":
                result["document"] = {"file_id": "cached-doc-" + str(self.next_id)}
            elif method == "sendVoice":
                result["voice"] = {"file_id": "cached-voice-" + str(self.next_id)}
            return self.ok(result)
        return JSONResponse({"ok": False, "error_code": 400, "description": f"unknown method {method}"}, status_code=400)

    @staticmethod
    def ok(result) -> JSONResponse:
        return JSONResponse({"ok": True, "result": result})

    def push_message(self, text: str = "", chat_id: int = GROUP_A, user_id: int = 555, name: str = "TG User",
                     message_id: int | None = None, **extra) -> int:
        mid = message_id or int(time.time() * 1000) % 100000000 + self.next_id + 1
        msg = {"message_id": mid, "date": int(time.time()),
               "chat": {"id": chat_id, "type": "supergroup", "title": TITLES.get(chat_id, "Mock TG")},
               "from": {"id": user_id, "is_bot": False, "first_name": name}}
        if text:
            msg["text"] = text
        msg.update(extra)
        self.updates.put_nowait({"update_id": int(time.time() * 1000000) % 1000000000, "message": msg})
        return mid


# --------------------------------------------------------------- helpers
async def wait_for(pred, timeout: float = 15.0, what: str = "condition"):
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = pred()
        if r:
            return r
        await asyncio.sleep(0.2)
    raise AssertionError(f"timeout waiting for {what}")


class Panel:
    """Drives the REST API like the web panel does."""

    def __init__(self, base: str):
        self.c = httpx.AsyncClient(base_url=base, timeout=30)
        self.csrf = ""

    async def req(self, method: str, path: str, **kw):
        headers = kw.pop("headers", {})
        if self.csrf and "X-CSRF-Token" not in headers:
            headers["X-CSRF-Token"] = self.csrf
        r = await self.c.request(method, "/api" + path, headers=headers, **kw)
        data = r.json()
        if r.status_code >= 400:
            raise RuntimeError(f"{method} {path} -> {r.status_code} {data}")
        return data


def sent_to(tg: MockTelegram, chat_id: int):
    return [x for x in tg.sent if str(x["params"].get("chat_id")) == str(chat_id) and x["method"].startswith("send")]


async def main() -> int:
    if _healthy(BRIDGE_PORT):
        print(f"port {BRIDGE_PORT} already serves a bridge instance; stop it first (fuser -k {BRIDGE_PORT}/tcp)")
        return 2
    home = Path(tempfile.mkdtemp(prefix="rb-e2e-"))
    env = {**os.environ, "QQTG_HOME": str(home), "PYTHONPATH": str(ROOT)}
    ffmpeg = os.environ.get("QQTG_FFMPEG") or shutil.which("ffmpeg") or ""
    if ffmpeg:
        env["QQTG_FFMPEG"] = ffmpeg
    token = subprocess.check_output([PY, "-m", "qqtg", "init", "--bind", "127.0.0.1", "--port", str(BRIDGE_PORT), "--quiet"], env=env, cwd=ROOT).decode().strip()

    tg = MockTelegram()
    tg_server = uvicorn.Server(uvicorn.Config(tg.app, host="127.0.0.1", port=TG_PORT, log_level="error"))
    tg_task = asyncio.create_task(tg_server.serve())
    await asyncio.sleep(0.5)

    proc = subprocess.Popen([PY, "-m", "qqtg", "run"], env=env, cwd=ROOT, stdout=open(home / "run.log", "w"), stderr=subprocess.STDOUT)
    failures: list[str] = []
    try:
        p = Panel(f"http://127.0.0.1:{BRIDGE_PORT}")
        await wait_for(lambda: proc.poll() is None and _healthy(BRIDGE_PORT), 20, "bridge start")

        # ---- setup + login
        s = await p.req("GET", "/session")
        assert s["needs_setup"], s
        r = await p.req("POST", "/setup", json={"setup_token": token, "username": "owner", "password": "password123"})
        p.csrf = r["csrf"]
        # wrong token must be rejected once configured
        try:
            await p.req("POST", "/setup", json={"setup_token": "x", "username": "a", "password": "b"})
            failures.append("setup allowed twice")
        except RuntimeError:
            pass
        await p.req("POST", "/logout")
        p.csrf = ""
        r = await p.req("POST", "/login", json={"username": "owner", "password": "password123"})
        p.csrf = r["csrf"]
        # CSRF enforcement
        try:
            await p.req("POST", "/chats/refresh", headers={"X-CSRF-Token": "bad"})
            failures.append("csrf not enforced")
        except RuntimeError:
            pass

        # ---- connections: Telegram only
        r = await p.req("PUT", "/connections/telegram", json={"token": TOKEN, "api_base": f"http://127.0.0.1:{TG_PORT}"})
        assert r["bot"]["username"] == "mock_bot"
        await asyncio.sleep(1.5)
        conns = await p.req("GET", "/connections")
        assert set(conns.keys()) == {"telegram"}, conns
        assert conns["telegram"]["status"]["connected"], conns
        assert conns["telegram"]["config"]["token_masked"].endswith(TOKEN[-4:]) and TOKEN not in json.dumps(conns)

        # ---- chat discovery via /bridge command in both groups
        tg.push_message("/bridge", chat_id=GROUP_A)
        tg.push_message("/bridge", chat_id=GROUP_B)
        await wait_for(lambda: len([x for x in tg.sent if "群组已识别" in x["params"].get("text", "")]) >= 2, 10, "/bridge replies")
        chats = (await p.req("GET", "/chats"))["chats"]
        chat_a = next(c for c in chats if c["platform"] == "telegram" and c["chat_id"] == str(GROUP_A))
        chat_b = next(c for c in chats if c["platform"] == "telegram" and c["chat_id"] == str(GROUP_B))
        assert chat_a["status"] == "pending" and chat_b["status"] == "pending", (chat_a, chat_b)

        # message before a bridge exists must NOT be forwarded (default safe)
        tg.push_message("no bridge yet", chat_id=GROUP_A, name="张三")
        await asyncio.sleep(1.0)
        assert not any("no bridge yet" in x["params"].get("text", "") for x in sent_to(tg, GROUP_B)), "forwarded without bridge!"

        # ---- create bridge A<->B (disabled), verify, test, then confirm disabled bridges stay quiet
        r = await p.req("POST", "/bridges", json={"name": "雨幕桥", "a_chat_id": chat_a["id"], "b_chat_id": chat_b["id"], "direction": "both", "enabled": False})
        bid = r["id"]
        v = await p.req("POST", f"/bridges/{bid}/verify")
        assert v["ok"] and v["a"]["ok"] and v["b"]["ok"], v
        t = await p.req("POST", f"/bridges/{bid}/test")
        assert t["results"]["a_to_b"]["ok"] and t["results"]["b_to_a"]["ok"], t
        base_a, base_b = len(sent_to(tg, GROUP_A)), len(sent_to(tg, GROUP_B))
        tg.push_message("still disabled", chat_id=GROUP_A, name="张三")
        await asyncio.sleep(1.0)
        assert len(sent_to(tg, GROUP_B)) == base_b, "forwarded while disabled!"
        await p.req("PATCH", f"/bridges/{bid}", json={"enabled": True})

        # ---- A -> B text
        mid_a = tg.push_message("你好 <b>雨幕</b>", chat_id=GROUP_A, name="张三")
        rec = await wait_for(lambda: next((x for x in sent_to(tg, GROUP_B) if x["method"] == "sendMessage" and "你好" in x["params"]["text"]), None), 10, "a->b text")
        assert rec["params"]["text"].startswith("<b>TG · 张三</b>"), rec["params"]["text"]
        assert "&lt;b&gt;雨幕&lt;/b&gt;" in rec["params"]["text"]
        # duplicate delivery of the same message id must be ignored
        before = len(sent_to(tg, GROUP_B))
        tg.push_message("你好 <b>雨幕</b>", chat_id=GROUP_A, name="张三", message_id=mid_a)
        await asyncio.sleep(1.0)
        assert len(sent_to(tg, GROUP_B)) == before, "duplicate not ignored"

        # ---- B -> A text
        tg.push_message("雨夜适合聊天", chat_id=GROUP_B, name="李四")
        rec_b = await wait_for(lambda: next((x for x in sent_to(tg, GROUP_A) if x["method"] == "sendMessage" and "雨夜适合聊天" in x["params"]["text"]), None), 10, "b->a text")
        assert rec_b["params"]["text"].startswith("<b>TG · 李四</b>"), rec_b["params"]["text"]

        # ---- reply mapping: B replies to the bridged copy -> becomes a reply to the original A message
        copy_in_b = rec["message_id"]
        tg.push_message(" reply to copy", chat_id=GROUP_B, name="李四",
                        reply_to_message={"message_id": copy_in_b, "text": rec["params"]["text"], "from": {"id": 999, "is_bot": True, "first_name": "MockBot"}})
        rp = await wait_for(lambda: next((x for x in sent_to(tg, GROUP_A) if x["method"] == "sendMessage" and "reply to copy" in x["params"]["text"]), None), 10, "reply mapped")
        reply_to = rp["params"].get("reply_to_message_id") or (rp["params"].get("reply_parameters") or {}).get("message_id")
        assert int(reply_to or 0) == mid_a, (rp["params"], mid_a)

        # ---- loop protection: bot's own message must not bounce back
        before_a = len(sent_to(tg, GROUP_A))
        tg.push_message("bot echo", chat_id=GROUP_B, user_id=999, name="MockBot")
        await asyncio.sleep(1.0)
        assert len(sent_to(tg, GROUP_A)) == before_a, "bot message bounced back!"
        # ... and a redelivered copy id (a message id we produced as target) must be dropped
        before_b = len(sent_to(tg, GROUP_B))
        tg.push_message("echo of copy", chat_id=GROUP_B, user_id=555, name="路人", message_id=copy_in_b)
        await asyncio.sleep(1.0)
        assert not any("echo of copy" in x["params"].get("text", "") for x in sent_to(tg, GROUP_A)[before_a:]), "loop not blocked"

        # ---- TG photo A -> B (multipart upload), second time via cached file_id
        tg.push_message("", chat_id=GROUP_A, name="张三",
                        photo=[{"file_id": "tgphoto1", "file_unique_id": "uq1", "width": 1, "height": 1, "file_size": len(PNG)}], caption="看图")
        ph = await wait_for(lambda: next((x for x in sent_to(tg, GROUP_B) if x["method"] == "sendPhoto"), None), 15, "a->b photo")
        assert "photo" in ph["files"] and ph["params"].get("caption", "").endswith("看图"), ph
        tg.push_message("", chat_id=GROUP_A, name="张三",
                        photo=[{"file_id": "tgphoto1", "file_unique_id": "uq1", "width": 1, "height": 1, "file_size": len(PNG)}])
        ph2 = await wait_for(lambda: next((x for x in sent_to(tg, GROUP_B) if x["method"] == "sendPhoto" and x is not ph), None), 15, "cached photo")
        assert not ph2["files"] and str(ph2["params"].get("photo", "")).startswith("cached-photo-"), ph2

        # ---- oversized TG document -> fallback text instead of dropping the message
        tg.push_message("", chat_id=GROUP_A, name="张三",
                        document={"file_id": "bigdoc", "file_unique_id": "ubig", "file_name": "movie.mp4",
                                  "file_size": 183 * 1024 * 1024, "mime_type": "video/mp4"}, caption="big file")
        fb = await wait_for(lambda: next((x for x in sent_to(tg, GROUP_B) if x["method"] == "sendMessage" and "movie.mp4" in x["params"]["text"]), None), 15, "big file fallback")
        assert "big file" in fb["params"]["text"], fb["params"]["text"]

        # ---- direction filter: a_to_b means B side messages stay local
        await p.req("PATCH", f"/bridges/{bid}", json={"direction": "a_to_b"})
        before_a = len(sent_to(tg, GROUP_A))
        tg.push_message("one way only", chat_id=GROUP_B, name="李四")
        await asyncio.sleep(1.2)
        assert len(sent_to(tg, GROUP_A)) == before_a, "direction filter failed"
        await p.req("PATCH", f"/bridges/{bid}", json={"direction": "both"})

        # ---- messages / logs / overview / diagnose / backup endpoints
        msgs = (await p.req("GET", "/messages?limit=50"))["messages"]
        assert any(m["status"] == "sent" and m["steps"] for m in msgs), msgs[:2]
        assert all(m["status"] != "dead" for m in msgs), [m for m in msgs if m["status"] == "dead"]
        directions = {m["direction"] for m in msgs}
        assert directions <= {"a_to_b", "b_to_a", "both"}, directions
        logs = (await p.req("GET", "/logs?limit=200"))["logs"]
        assert logs and TOKEN not in json.dumps(logs), "token leaked into logs"
        ov = await p.req("GET", "/overview")
        assert ov["health"]["components"]["adapter_telegram"]["ok"], ov["health"]["components"]
        assert ov["stats"]["today"]["sent"] >= 5, ov["stats"]["today"]
        assert ov["stats"]["today"]["a_to_b"] >= 1 and ov["stats"]["today"]["b_to_a"] >= 1, ov["stats"]["today"]
        diag = (await p.req("POST", "/diagnose"))["checks"]
        assert all(c["status"] != "FAIL" for c in diag if c["name"] in ("TELEGRAM Connection", "Database")), diag
        br = (await p.req("GET", "/bridges"))["bridges"]
        assert br and br[0]["a_chat"] and br[0]["b_chat"] and br[0]["a_platform"] == "telegram", br
        bk = await p.c.get("/api/backup", headers={"X-CSRF-Token": p.csrf})
        assert bk.status_code == 200 and bk.json()["bridges"], bk.status_code
        # restored-import of our own backup must not duplicate the bridge
        r = await p.req("POST", "/restore", json=bk.json())
        assert (await p.req("GET", "/bridges"))["bridges"].__len__() == 1

        print("  all bridge checks OK")

        if os.environ.get("E2E_KEEP") and not failures:
            print(f"READY http://127.0.0.1:{BRIDGE_PORT}  (owner / password123) - Ctrl-C to stop", flush=True)
            while True:
                await asyncio.sleep(3600)
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    except Exception as exc:  # noqa: BLE001
        import traceback
        traceback.print_exc()
        failures.append(f"{type(exc).__name__}: {exc}")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        tg_server.should_exit = True
        await tg_task

    log = (home / "run.log").read_text(encoding="utf-8", errors="replace")
    if failures:
        print("\n---- bridge log (tail) ----")
        print("\n".join(log.splitlines()[-60:]))
        print("\nFAILURES:")
        for f in failures:
            print(" -", f)
        return 1
    if "Traceback" in log:
        print("\n---- unexpected traceback in bridge log ----")
        print(log)
        return 1
    print(f"\nALL E2E CHECKS PASSED  (tg sent={len(tg.sent)})")
    shutil.rmtree(home, ignore_errors=True)
    return 0


def _healthy(port: int) -> bool:
    try:
        return httpx.get(f"http://127.0.0.1:{port}/healthz", timeout=2).status_code == 200
    except Exception:
        return False


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
