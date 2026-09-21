"""End-to-end smoke test with a mock OneBot server and a mock Telegram Bot API.

Run:  python tests/e2e_mock.py  (needs the project's dependencies installed)

It boots the real bridge (``qqtg run``) against the mocks, drives the REST API
exactly like the panel does, and asserts that messages flow both ways with
loop protection / deduplication / media conversion.
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
import uuid
from pathlib import Path

import httpx
import uvicorn
import websockets
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse

ROOT = Path(__file__).resolve().parent.parent
PY = sys.executable
BRIDGE_PORT = 18321
TG_PORT = 18322
OB_PORT = 18323
TOKEN = "123456789:AAFakeTokenForTestsOnly_ABCDEFGHIJKLMNOPQ"

# 1x1 PNG
PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==")


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
            form = await request.form()
            params = {}
            files = {}
            for k, v in form.multi_items():
                if hasattr(v, "filename"):
                    files[k] = {"filename": v.filename, "size": len(await v.read()), "content_type": v.content_type}
                else:
                    params[k] = v
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
            return self.ok({"id": int(params["chat_id"]), "type": "supergroup", "title": "Mock TG Group", "permissions": {"can_send_messages": True, "can_send_photos": True, "can_send_videos": True, "can_send_documents": True, "can_send_audios": True, "can_send_voice_notes": True, "can_send_other_messages": True}})
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
            result = {"message_id": self.next_id, "chat": {"id": int(params["chat_id"]), "type": "supergroup"}, "date": int(time.time()), "from": {"id": 999, "is_bot": True, "first_name": "MockBot"}}
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

    def push_message(self, text: str, chat_id: int = -1001234, user_id: int = 555, name: str = "TG User", message_id: int | None = None, **extra) -> int:
        mid = message_id or int(time.time() * 1000) % 1000000
        msg = {"message_id": mid, "date": int(time.time()), "chat": {"id": chat_id, "type": "supergroup", "title": "Mock TG Group"},
               "from": {"id": user_id, "is_bot": False, "first_name": name}}
        if text:
            msg["text"] = text
        msg.update(extra)
        self.updates.put_nowait({"update_id": int(time.time() * 1000000) % 100000000, "message": msg})
        return mid


# -------------------------------------------------------------- mock OneBot
class MockOneBot:
    def __init__(self) -> None:
        self.sent: list[dict] = []
        self.clients: set = set()
        self.next_id = 500

    async def handler(self, ws) -> None:
        self.clients.add(ws)
        try:
            await ws.send(json.dumps({"post_type": "meta_event", "meta_event_type": "lifecycle", "sub_type": "connect", "self_id": 10001, "time": int(time.time())}))
            async for raw in ws:
                data = json.loads(raw)
                action, params, echo = data.get("action"), data.get("params", {}), data.get("echo")
                result = None
                if action == "get_login_info":
                    result = {"user_id": 10001, "nickname": "MockQQ"}
                elif action == "get_version_info":
                    result = {"app_name": "MockOneBot", "app_version": "1.0"}
                elif action == "get_group_list":
                    result = [{"group_id": 123456, "group_name": "测试群", "member_count": 10}, {"group_id": 654321, "group_name": "闲聊群", "member_count": 3}]
                elif action == "get_group_member_info":
                    result = {"user_id": params.get("user_id"), "role": "member", "shut_up_timestamp": 0, "card": "", "nickname": "MockQQ"}
                elif action == "send_group_msg":
                    self.next_id += 1
                    self.sent.append({"group_id": params["group_id"], "message": params["message"], "message_id": self.next_id})
                    result = {"message_id": self.next_id}
                elif action == "get_msg":
                    result = {"message_id": params["message_id"], "sender": {"user_id": 20002, "nickname": "李四"}, "message": [{"type": "text", "data": {"text": "原始消息内容"}}]}
                elif action == "delete_msg":
                    self.sent.append({"deleted": params["message_id"]})
                    result = None
                else:
                    await ws.send(json.dumps({"status": "failed", "retcode": 1404, "message": f"unsupported {action}", "echo": echo}))
                    continue
                await ws.send(json.dumps({"status": "ok", "retcode": 0, "data": result, "echo": echo}))
        finally:
            self.clients.discard(ws)

    async def push_group_message(self, segments: list[dict], group_id: int = 123456, user_id: int = 20002, name: str = "张三", message_id: int | None = None) -> int:
        mid = message_id or int(time.time() * 1000) % 1000000
        ev = {"post_type": "message", "message_type": "group", "sub_type": "normal", "message_id": mid, "group_id": group_id, "user_id": user_id,
              "sender": {"user_id": user_id, "nickname": name, "card": ""}, "message": segments, "raw_message": "", "time": int(time.time()), "self_id": 10001}
        for ws in list(self.clients):
            await ws.send(json.dumps(ev, ensure_ascii=False))
        return mid

    async def push_event(self, ev: dict) -> None:
        for ws in list(self.clients):
            await ws.send(json.dumps(ev, ensure_ascii=False))


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


async def main() -> int:
    if _healthy(BRIDGE_PORT):
        print(f"port {BRIDGE_PORT} already serves a bridge instance; stop it first (fuser -k {BRIDGE_PORT}/tcp)")
        return 2
    home = Path(tempfile.mkdtemp(prefix="qqtg-e2e-"))
    env = {**os.environ, "QQTG_HOME": str(home), "PYTHONPATH": str(ROOT)}
    ffmpeg = os.environ.get("QQTG_FFMPEG") or shutil.which("ffmpeg") or ""
    if ffmpeg:
        env["QQTG_FFMPEG"] = ffmpeg
    token = subprocess.check_output([PY, "-m", "qqtg", "init", "--bind", "127.0.0.1", "--port", str(BRIDGE_PORT), "--quiet"], env=env, cwd=ROOT).decode().strip()

    tg = MockTelegram()
    ob = MockOneBot()
    tg_server = uvicorn.Server(uvicorn.Config(tg.app, host="127.0.0.1", port=TG_PORT, log_level="error"))
    tg_task = asyncio.create_task(tg_server.serve())
    ob_server = await websockets.serve(ob.handler, "127.0.0.1", OB_PORT)
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

        # ---- connections
        r = await p.req("PUT", "/connections/telegram", json={"token": TOKEN, "api_base": f"http://127.0.0.1:{TG_PORT}"})
        assert r["bot"]["username"] == "mock_bot"
        await p.req("PUT", "/connections/qq", json={"mode": "forward", "ws_url": f"ws://127.0.0.1:{OB_PORT}", "access_token": ""})
        await wait_for(lambda: bool(ob.clients), 10, "onebot connect")
        await asyncio.sleep(1.0)
        conns = await p.req("GET", "/connections")
        assert conns["qq"]["status"]["connected"] and conns["telegram"]["status"]["connected"], conns
        assert conns["telegram"]["config"]["token_masked"].endswith(TOKEN[-4:]) and TOKEN not in json.dumps(conns)

        # ---- chat discovery: QQ groups from get_group_list, TG from /bridge command
        chats = (await p.req("GET", "/chats"))["chats"]
        assert any(c["platform"] == "qq" and c["chat_id"] == "123456" for c in chats), chats
        tg.push_message("/bridge", chat_id=-1001234)
        await wait_for(lambda: any(x["method"] == "sendMessage" and "群组已识别" in x["params"]["text"] for x in tg.sent), 10, "/bridge reply")
        chats = (await p.req("GET", "/chats"))["chats"]
        tg_chat = next(c for c in chats if c["platform"] == "telegram" and c["chat_id"] == "-1001234")
        qq_chat = next(c for c in chats if c["platform"] == "qq" and c["chat_id"] == "123456")
        assert tg_chat["status"] == "pending", tg_chat

        # message before a bridge exists must NOT be forwarded (default safe)
        await ob.push_group_message([{"type": "text", "data": {"text": "no bridge yet"}}])
        await asyncio.sleep(1.0)
        assert not any("no bridge yet" in json.dumps(x, ensure_ascii=False) for x in tg.sent), "forwarded without bridge!"

        # ---- create bridge (disabled), verify, test, enable
        r = await p.req("POST", "/bridges", json={"name": "测试桥", "qq_chat_id": qq_chat["id"], "tg_chat_id": tg_chat["id"], "direction": "both", "enabled": False})
        bid = r["id"]
        v = await p.req("POST", f"/bridges/{bid}/verify")
        assert v["ok"], v
        t = await p.req("POST", f"/bridges/{bid}/test")
        assert t["results"]["qq_to_tg"]["ok"] and t["results"]["tg_to_qq"]["ok"], t
        await ob.push_group_message([{"type": "text", "data": {"text": "still disabled"}}])
        await asyncio.sleep(1.0)
        assert not any("still disabled" in json.dumps(x, ensure_ascii=False) for x in tg.sent), "forwarded while disabled!"
        await p.req("PATCH", f"/bridges/{bid}", json={"enabled": True})

        # ---- QQ -> TG text
        mid = await ob.push_group_message([{"type": "text", "data": {"text": "你好 <b>Telegram</b>"}}, {"type": "face", "data": {"id": 14}}])
        rec = await wait_for(lambda: next((x for x in tg.sent if x["method"] == "sendMessage" and "Telegram" in x["params"]["text"]), None), 10, "qq->tg text")
        assert rec["params"]["text"].startswith("<b>QQ · 张三</b>"), rec["params"]["text"]
        assert "&lt;b&gt;Telegram&lt;/b&gt;" in rec["params"]["text"] and "[微笑]" in rec["params"]["text"]
        # duplicate delivery of the same QQ message id must be ignored
        before = len(tg.sent)
        await ob.push_group_message([{"type": "text", "data": {"text": "你好 <b>Telegram</b>"}}], message_id=mid)
        await asyncio.sleep(1.0)
        assert len(tg.sent) == before, "duplicate not ignored"

        # ---- TG -> QQ text with reply to the bridged copy -> becomes a QQ reply segment
        tg_mid = tg.push_message("hello QQ", reply_to_message=({"message_id": rec["message_id"], "text": rec["params"]["text"], "from": {"id": 999, "is_bot": True, "first_name": "MockBot"}}))
        qrec = await wait_for(lambda: next((x for x in ob.sent if "message" in x and any(s["type"] == "text" and "hello QQ" in s["data"]["text"] for s in x["message"])), None), 10, "tg->qq text")
        assert qrec["group_id"] == 123456
        assert qrec["message"][0]["type"] == "reply" and qrec["message"][0]["data"]["id"] == str(mid), qrec

        # ---- loop protection: bot's own TG message must not bounce back
        before = len(ob.sent)
        tg.push_message("bot echo", user_id=999, name="MockBot")
        await asyncio.sleep(1.0)
        assert len(ob.sent) == before, "bot message bounced back!"
        # ... and a QQ message id that is one of our own targets must be dropped
        tg_copy_id = qrec["message_id"]
        await ob.push_group_message([{"type": "text", "data": {"text": "hello QQ"}}], user_id=10001, message_id=tg_copy_id)
        await asyncio.sleep(1.0)
        assert not any(x["method"] == "sendMessage" and x["params"]["text"].endswith("hello QQ") and x["message_id"] > rec["message_id"] + 1 for x in tg.sent[-3:]), "loop!"

        # ---- QQ reply preview + at
        await ob.push_group_message([{"type": "reply", "data": {"id": "77"}}, {"type": "at", "data": {"qq": "20002", "name": "李四"}}, {"type": "text", "data": {"text": " 同意"}}])
        rr = await wait_for(lambda: next((x for x in tg.sent if x["method"] == "sendMessage" and "同意" in x["params"]["text"]), None), 10, "qq reply")
        assert "↪ 李四：原始消息内容" in rr["params"]["text"] and "@李四" in rr["params"]["text"], rr["params"]["text"]

        # ---- QQ image (url) -> TG sendPhoto (multipart upload), second time via cached file_id
        await ob.push_group_message([{"type": "image", "data": {"file": "abcdef0123456789.png", "url": f"http://127.0.0.1:{TG_PORT}/_media/test.png", "sub_type": 0}}, {"type": "text", "data": {"text": "看图"}}])
        ph = await wait_for(lambda: next((x for x in tg.sent if x["method"] == "sendPhoto"), None), 15, "qq->tg photo")
        assert "photo" in ph["files"] and ph["params"].get("caption", "").endswith("看图"), ph
        await ob.push_group_message([{"type": "image", "data": {"file": "abcdef0123456789.png", "url": f"http://127.0.0.1:{TG_PORT}/_media/test.png", "sub_type": 0}}])
        ph2 = await wait_for(lambda: next((x for x in tg.sent if x["method"] == "sendPhoto" and x is not ph), None), 15, "cached photo")
        assert not ph2["files"] and str(ph2["params"].get("photo", "")).startswith("cached-photo-"), ph2

        # ---- TG photo -> QQ image segment (base64)
        tg.push_message("", photo=[{"file_id": "tgphoto1", "file_unique_id": "uq1", "width": 1, "height": 1, "file_size": len(PNG)}], caption="tg pic")
        qp = await wait_for(lambda: next((x for x in ob.sent if "message" in x and any(s["type"] == "image" for s in x["message"])), None), 15, "tg->qq photo")
        img = next(s for s in qp["message"] if s["type"] == "image")
        assert img["data"]["file"].startswith("base64://") and base64.b64decode(img["data"]["file"][9:]) == PNG

        # ---- TG document that is too big -> fallback text instead of dropping the message
        tg.push_message("", document={"file_id": "bigdoc", "file_unique_id": "ubig", "file_name": "movie.mp4", "file_size": 183 * 1024 * 1024, "mime_type": "video/mp4"}, caption="big file")
        qb = await wait_for(lambda: next((x for x in ob.sent if "message" in x and any(s["type"] == "text" and "movie.mp4" in s["data"]["text"] for s in x["message"])), None), 15, "big file fallback")
        txt = next(s["data"]["text"] for s in qb["message"] if s["type"] == "text")
        assert "big file" in txt and "下载上限" in txt, txt

        # ---- recall sync (enable option first)
        await p.req("PATCH", f"/bridges/{bid}", json={"options": {"recall_sync": True}})
        mid2 = await ob.push_group_message([{"type": "text", "data": {"text": "will be recalled"}}])
        rec2 = await wait_for(lambda: next((x for x in tg.sent if x["method"] == "sendMessage" and "will be recalled" in x["params"]["text"]), None), 10, "recall source")
        await ob.push_event({"post_type": "notice", "notice_type": "group_recall", "group_id": 123456, "user_id": 20002, "operator_id": 20002, "message_id": mid2, "time": int(time.time()), "self_id": 10001})
        await wait_for(lambda: any(x.get("method") == "deleteMessage" and int(x["params"]["message_id"]) == rec2["message_id"] for x in tg.sent), 10, "recall sync")

        # ---- direction filter
        await p.req("PATCH", f"/bridges/{bid}", json={"direction": "qq_to_tg"})
        before = len(ob.sent)
        tg.push_message("one way only")
        await asyncio.sleep(1.2)
        assert len(ob.sent) == before, "direction filter failed"
        await p.req("PATCH", f"/bridges/{bid}", json={"direction": "both"})

        # ---- messages / logs / overview / diagnose / backup endpoints
        msgs = (await p.req("GET", "/messages?limit=50"))["messages"]
        assert any(m["status"] == "sent" and m["steps"] for m in msgs), msgs[:2]
        assert all(m["status"] != "dead" for m in msgs), [m for m in msgs if m["status"] == "dead"]
        logs = (await p.req("GET", "/logs?limit=200"))["logs"]
        assert logs and TOKEN not in json.dumps(logs), "token leaked into logs"
        ov = await p.req("GET", "/overview")
        assert ov["health"]["components"]["qq"]["ok"] and ov["stats"]["today"]["sent"] >= 5, ov["stats"]
        diag = (await p.req("POST", "/diagnose"))["checks"]
        assert all(c["status"] != "FAIL" for c in diag if c["name"] in ("QQ Connection", "TG Connection", "Database")), diag
        bk = await p.c.get("/api/backup", headers={"X-CSRF-Token": p.csrf})
        assert bk.status_code == 200 and TOKEN not in bk.text and "config_enc" not in bk.text
        bk2 = await p.c.get("/api/backup?with_secrets=1", headers={"X-CSRF-Token": p.csrf})
        assert TOKEN not in bk2.text and "config_enc" in bk2.text

        # ---- role enforcement: viewer cannot mutate
        await p.req("POST", "/users", json={"username": "viewer1", "password": "password123", "role": "viewer"})
        pv = Panel(f"http://127.0.0.1:{BRIDGE_PORT}")
        r = await pv.req("POST", "/login", json={"username": "viewer1", "password": "password123"})
        pv.csrf = r["csrf"]
        await pv.req("GET", "/bridges")
        try:
            await pv.req("PATCH", f"/bridges/{bid}", json={"enabled": False})
            failures.append("viewer could mutate")
        except RuntimeError:
            pass
        # login throttle (skipped in keep mode so the UI smoke test can still log in from this IP)
        for _ in range(0 if os.environ.get("E2E_KEEP") else 5):
            try:
                await pv.req("POST", "/login", json={"username": "viewer1", "password": "wrong"})
            except RuntimeError:
                pass
        if not os.environ.get("E2E_KEEP"):
            try:
                await pv.req("POST", "/login", json={"username": "viewer1", "password": "password123"})
                failures.append("login throttle missing")
            except RuntimeError as exc:
                assert "429" in str(exc), exc

        # ---- voice conversion (only when ffmpeg is available)
        if ffmpeg:
            ogg = home / "v.ogg"
            subprocess.run([ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=1", "-c:a", "libopus", str(ogg)], check=True)
            tg.files["voice/v.ogg"] = ogg.read_bytes()
            orig_getfile = tg.files
            del orig_getfile
            # make getFile point at the voice for this file id
            async def handle_voice(token: str, method: str, request: Request):
                return await MockTelegram.handle(tg, token, method, request)
            tg.files["photos/p1.jpg"] = ogg.read_bytes()  # getFile always returns photos/p1.jpg in this mock
            tg.push_message("", voice={"file_id": "voice1", "file_unique_id": "uv1", "duration": 1, "mime_type": "audio/ogg", "file_size": ogg.stat().st_size})
            qv = await wait_for(lambda: next((x for x in ob.sent if "message" in x and any(s["type"] == "record" for s in x["message"])), None), 30, "tg->qq voice")
            rec_seg = next(s for s in qv["message"] if s["type"] == "record")
            raw = base64.b64decode(rec_seg["data"]["file"][9:])
            assert raw[:4] == b"RIFF" and raw[8:12] == b"WAVE", raw[:16]
            tg.files["photos/p1.jpg"] = PNG
            print("  voice ogg -> wav conversion OK")
        else:
            print("  (ffmpeg not found, skipping voice conversion test)")

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
        ob_server.close()
        await ob_server.wait_closed()
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
    print(f"\nALL E2E CHECKS PASSED  (tg sent={len(tg.sent)}, qq sent={len(ob.sent)})")
    shutil.rmtree(home, ignore_errors=True)
    return 0


def _healthy(port: int) -> bool:
    try:
        return httpx.get(f"http://127.0.0.1:{port}/healthz", timeout=2).status_code == 200
    except Exception:
        return False


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
