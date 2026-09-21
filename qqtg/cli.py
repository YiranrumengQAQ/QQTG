"""Command line interface: ``qqtg <command>``."""
from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import os
import shutil
import sys
import time
from pathlib import Path

from . import __version__
from .config import Config, generate_secret_key, load_config, write_env_file
from .db import Database, sync_backup
from .logsys import setup_logging
from .security import hash_password, mask_secret


def _print_banner() -> None:
    print("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    print(f" Rain Bridge · 群组桥接  v{__version__}")
    print("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")


def cmd_init(cfg: Config, args: argparse.Namespace) -> int:
    """Create directories, bootstrap config and the database; print a setup token."""
    cfg.ensure_dirs()
    values: dict[str, str] = {}
    if not cfg.secret_key:
        cfg.secret_key = generate_secret_key()
        values["QQTG_SECRET_KEY"] = cfg.secret_key
    if args.bind:
        values["QQTG_BIND"] = args.bind
        cfg.bind = args.bind
    if args.port:
        values["QQTG_PORT"] = str(args.port)
        cfg.port = args.port
    if args.public_url:
        values["QQTG_PUBLIC_URL"] = args.public_url
    if not cfg.env_file.exists() or values:
        write_env_file(cfg, {"QQTG_HOME": str(cfg.home), "QQTG_BIND": cfg.bind, "QQTG_PORT": str(cfg.port), **values})

    async def _run() -> str:
        from .core.app import BridgeApp
        app = BridgeApp(cfg)
        await app.db.open()
        await app.settings.load()
        token = ""
        if await app.needs_setup():
            token = await app.generate_setup_token()
        await app.db.close()
        return token

    token = asyncio.run(_run())
    if not args.quiet:
        _print_banner()
        print(f"目录:        {cfg.home}")
        print(f"配置:        {cfg.env_file}")
        print(f"数据库:      {cfg.db_path}")
        print(f"监听:        http://{cfg.bind}:{cfg.port}")
        if token:
            print()
            print("首次访问面板需要以下初始化令牌（仅显示一次，可用 `qqtg setup-token` 重新生成）:")
            print()
            print(f"    {token}")
            print()
        else:
            print("管理员账号已存在，无需初始化令牌。")
    elif token:
        print(token)
    return 0


def cmd_run(cfg: Config, args: argparse.Namespace) -> int:
    if not cfg.secret_key:
        print("错误: QQTG_SECRET_KEY 未配置，请先运行 `qqtg init`", file=sys.stderr)
        return 2
    cfg.ensure_dirs()
    setup_logging(cfg.logs_dir, cfg.log_level)
    from .core.app import BridgeApp
    from .web.app import run
    run(BridgeApp(cfg))
    return 0


def cmd_setup_token(cfg: Config, args: argparse.Namespace) -> int:
    async def _run() -> int:
        from .core.app import BridgeApp
        app = BridgeApp(cfg)
        await app.db.open()
        await app.settings.load()
        if not await app.needs_setup() and not args.force:
            print("管理员账号已存在。如需重置密码请使用: qqtg reset-password <用户名>")
            print("如确实要重新初始化（删除所有账号），请加 --force")
            await app.db.close()
            return 1
        if args.force:
            await app.db.execute("DELETE FROM sessions")
            await app.db.execute("DELETE FROM users")
        token = await app.generate_setup_token()
        await app.db.close()
        print("初始化令牌（首次访问面板时输入）:")
        print()
        print(f"    {token}")
        return 0

    return asyncio.run(_run())


def cmd_reset_password(cfg: Config, args: argparse.Namespace) -> int:
    async def _run() -> int:
        db = Database(cfg.db_path)
        await db.open()
        user = await db.fetchone("SELECT id FROM users WHERE username=?", (args.username,))
        if not user:
            print(f"用户不存在: {args.username}")
            await db.close()
            return 1
        pw = args.password or getpass.getpass("新密码: ")
        if len(pw) < 8:
            print("密码至少 8 位")
            await db.close()
            return 1
        await db.update("users", {"password_hash": hash_password(pw)}, "id=?", (user["id"],))
        await db.execute("DELETE FROM sessions WHERE user_id=?", (user["id"],))
        await db.close()
        print(f"已重置 {args.username} 的密码")
        return 0

    return asyncio.run(_run())


def cmd_status(cfg: Config, args: argparse.Namespace) -> int:
    import urllib.request

    _print_banner()
    print(f"目录:      {cfg.home}")
    print(f"监听:      http://{cfg.bind}:{cfg.port}")
    url = f"http://{'127.0.0.1' if cfg.bind in ('0.0.0.0', '::') else cfg.bind}:{cfg.port}/healthz"
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:  # noqa: S310
            data = json.loads(resp.read().decode())
            print(f"服务:      ● 运行中 (v{data.get('version')})")
    except Exception as exc:
        print(f"服务:      ○ 未响应 ({type(exc).__name__})")

    async def _run() -> None:
        db = Database(cfg.db_path)
        await db.open()
        conns = await db.fetchall("SELECT platform, name, self_id, self_name, enabled FROM connections")
        print("\n连接:")
        if not conns:
            print("  (尚未配置，请在面板中连接 Telegram)")
        for c in conns:
            print(f"  {c['platform']:<9} {c['self_name'] or c['name'] or ''} {('(' + str(c['self_id']) + ')') if c['self_id'] else ''}")
        bridges = await db.fetchall(
            "SELECT b.id, b.name, b.enabled, b.direction, ca.title AS a_title, cb.title AS b_title FROM bridges b "
            "JOIN chats ca ON ca.id=b.a_chat_id JOIN chats cb ON cb.id=b.b_chat_id ORDER BY b.id")
        print("\n桥接:")
        if not bridges:
            print("  (无)")
        arrows = {"both": "↔", "a_to_b": "→", "b_to_a": "←"}
        for b in bridges:
            print(f"  #{b['id']:<3} {'●' if b['enabled'] else '○'} {b['name']}: {b['a_title']} {arrows.get(b['direction'], '?')} {b['b_title']}")
        today = time.strftime("%Y-%m-%d")
        st = await db.fetchone("SELECT COALESCE(SUM(sent),0) AS sent, COALESCE(SUM(failed),0) AS failed FROM stats_daily WHERE day=?", (today,))
        print(f"\n今日消息: {st['sent'] if st else 0} 成功 / {st['failed'] if st else 0} 失败")
        await db.close()

    asyncio.run(_run())
    return 0


def cmd_diagnose(cfg: Config, args: argparse.Namespace) -> int:
    _print_banner()
    rows: list[tuple[str, str, str]] = []

    def add(name: str, ok: bool, detail: str = "", warn: bool = False) -> None:
        rows.append((name, "PASS" if ok else ("WARN" if warn else "FAIL"), detail))

    add("Python", sys.version_info >= (3, 10), sys.version.split()[0])
    ff = shutil.which(cfg.ffmpeg)
    add("FFmpeg", bool(ff), ff or "未安装 (语音/贴纸/动画转换不可用)", warn=True)
    add("FFprobe", bool(shutil.which(cfg.ffprobe)), shutil.which(cfg.ffprobe) or "缺失，将使用 ffmpeg 回退", warn=True)
    add("Config", cfg.env_file.exists(), str(cfg.env_file))
    add("Secret key", bool(cfg.secret_key), "已配置" if cfg.secret_key else "缺失，请运行 qqtg init")
    add("Database", cfg.db_path.exists(), str(cfg.db_path))
    for d in (cfg.data_dir, cfg.logs_dir, cfg.tmp_dir):
        add(f"Writable {d.name}/", os.access(d, os.W_OK) if d.exists() else False, str(d))
    try:
        st = os.statvfs(cfg.home)
        free = st.f_bavail * st.f_frsize // 1024 // 1024
        add("Disk free", free > 500, f"{free} MB", warn=free > 200)
    except OSError:
        pass
    import urllib.request
    url = f"http://{'127.0.0.1' if cfg.bind in ('0.0.0.0', '::') else cfg.bind}:{cfg.port}/healthz"
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:  # noqa: S310
            add("Service", resp.status == 200, url)
    except Exception as exc:
        add("Service", False, f"{url} ({type(exc).__name__})")
    width = max(len(r[0]) for r in rows) + 2
    for name, status, detail in rows:
        print(f"{name:<{width}}{status:<6}{detail}")
    print("\n完整诊断（连接/桥接/权限）请在 Web 面板「系统 → 一键诊断」中运行。")
    return 0 if all(r[1] != "FAIL" for r in rows) else 1


def cmd_backup(cfg: Config, args: argparse.Namespace) -> int:
    async def _run() -> int:
        from .core.app import BridgeApp
        app = BridgeApp(cfg)
        await app.db.open()
        await app.settings.load()
        data = await app.export_backup(with_secrets=args.with_secrets)
        await app.db.close()
        path = Path(args.path or f"qqtg-backup-{time.strftime('%Y%m%d-%H%M%S')}.json")
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.chmod(path, 0o600)
        print(f"已导出备份: {path}")
        if not args.with_secrets:
            print("（未包含 Bot Token；恢复后需重新输入。使用 --with-secrets 可导出加密后的凭据，仅能在相同 QQTG_SECRET_KEY 下恢复）")
        return 0

    return asyncio.run(_run())


def cmd_restore(cfg: Config, args: argparse.Namespace) -> int:
    async def _run() -> int:
        from .core.app import BridgeApp
        data = json.loads(Path(args.path).read_text(encoding="utf-8"))
        app = BridgeApp(cfg)
        await app.db.open()
        await app.settings.load()
        counts = await app.import_backup(data)
        await app.db.close()
        print("恢复完成:")
        for k, v in counts.items():
            print(f"  ✓ {k}: {v}")
        print("如服务正在运行，请执行 systemctl restart qqtg-bridge 使连接配置生效。")
        return 0

    return asyncio.run(_run())


def cmd_db_backup(cfg: Config, args: argparse.Namespace) -> int:
    dst = Path(args.path or (cfg.data_dir / "backups" / f"bridge-{time.strftime('%Y%m%d-%H%M%S')}.db"))
    sync_backup(cfg.db_path, dst)
    print(str(dst))
    return 0


def cmd_migrate(cfg: Config, args: argparse.Namespace) -> int:
    async def _run() -> int:
        db = Database(cfg.db_path)
        await db.open()
        v = await db.current_version()
        await db.close()
        print(f"数据库版本: {v}")
        return 0

    return asyncio.run(_run())


def cmd_config(cfg: Config, args: argparse.Namespace) -> int:
    print(json.dumps({
        "home": str(cfg.home), "config": str(cfg.env_file), "db": str(cfg.db_path), "logs": str(cfg.logs_dir),
        "tmp": str(cfg.tmp_dir), "bind": cfg.bind, "port": cfg.port, "public_url": cfg.public_url,
        "secret_key": mask_secret(cfg.secret_key), "ffmpeg": shutil.which(cfg.ffmpeg) or cfg.ffmpeg,
    }, ensure_ascii=False, indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="qqtg", description="Rain Bridge · 群组桥接")
    parser.add_argument("--home", help="安装目录 (默认 $QQTG_HOME 或 /opt/qqtg-bridge)")
    parser.add_argument("--version", action="version", version=f"qqtg-bridge {__version__}")
    sub = parser.add_subparsers(dest="command")

    p = sub.add_parser("init", help="初始化目录、配置与数据库")
    p.add_argument("--bind")
    p.add_argument("--port", type=int)
    p.add_argument("--public-url")
    p.add_argument("--quiet", action="store_true", help="仅输出初始化令牌")
    p.set_defaults(func=cmd_init)

    sub.add_parser("run", help="启动服务 (systemd 使用)").set_defaults(func=cmd_run)

    p = sub.add_parser("setup-token", help="生成/重新生成初始化令牌")
    p.add_argument("--force", action="store_true", help="删除所有账号并重新初始化")
    p.set_defaults(func=cmd_setup_token)

    p = sub.add_parser("reset-password", help="重置面板用户密码")
    p.add_argument("username")
    p.add_argument("--password")
    p.set_defaults(func=cmd_reset_password)

    sub.add_parser("status", help="查看状态").set_defaults(func=cmd_status)
    sub.add_parser("diagnose", help="本地诊断").set_defaults(func=cmd_diagnose)

    p = sub.add_parser("backup", help="导出配置备份 (JSON)")
    p.add_argument("path", nargs="?")
    p.add_argument("--with-secrets", action="store_true")
    p.set_defaults(func=cmd_backup)

    p = sub.add_parser("restore", help="从 JSON 备份恢复")
    p.add_argument("path")
    p.set_defaults(func=cmd_restore)

    p = sub.add_parser("db-backup", help="备份 SQLite 数据库文件")
    p.add_argument("path", nargs="?")
    p.set_defaults(func=cmd_db_backup)

    sub.add_parser("migrate", help="执行数据库迁移").set_defaults(func=cmd_migrate)
    sub.add_parser("config", help="显示当前配置").set_defaults(func=cmd_config)

    args = parser.parse_args(argv)
    if not args.command:
        parser.print_help()
        return 0
    try:
        cfg = load_config(args.home)
        return int(args.func(cfg, args) or 0)
    except PermissionError as exc:
        print(f"权限不足: {exc}\n请使用 root 运行（例如: sudo qqtg {args.command}），命令会自动切换到服务用户。", file=sys.stderr)
        return 13
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
