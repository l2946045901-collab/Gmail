"""邮件中心 CLI。

用法：python -m mail_center.cli <命令组> <子命令> [参数]
命令组：account / profile / template / blacklist / mail / campaign / track / leads / poll / run
"""

import argparse
import getpass
import logging
import sys

from . import config, db
from .engine import scheduler
from .services import accounts, blacklist, campaigns, direct_mail, inbox, templates, tracking

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("mailcenter.cli")

LEVEL_LABELS = {"high": "高", "medium": "中", "low": "低", "none": "无"}


# ---------------------------------------------------------------------------
# 输出辅助
# ---------------------------------------------------------------------------

def print_table(rows: list[dict], columns: list[tuple[str, str]]) -> None:
    """columns: [(key, header), ...]"""
    if not rows:
        print("（无数据）")
        return
    widths = {}
    for key, header in columns:
        widths[key] = max(len(header), max(len(str(r.get(key, ""))) for r in rows))
    header_line = " | ".join(h.ljust(widths[k]) for k, h in columns)
    print(header_line)
    print("-+-".join("-" * widths[k] for k, _ in columns))
    for r in rows:
        print(" | ".join(str(r.get(k, "")).ljust(widths[k]) for k, _ in columns))


def pick_account(account_id=None) -> dict:
    if account_id:
        acc = accounts.get_account(account_id)
        if not acc:
            sys.exit(f"错误：找不到账号 #{account_id}")
        return acc
    accs = accounts.list_accounts(active_only=True)
    if not accs:
        sys.exit("错误：还没有绑定任何邮箱，请先执行 account add")
    if len(accs) == 1:
        return accs[0]
    print("请选择发件账号：")
    for a in accs:
        print(f"  [{a['id']}] {a['name']} <{a['email']}>")
    choice = input("输入账号编号：").strip()
    try:
        return next(a for a in accs if a["id"] == int(choice))
    except (ValueError, StopIteration):
        sys.exit("无效的账号编号")


# ---------------------------------------------------------------------------
# account 命令组
# ---------------------------------------------------------------------------

def cmd_account_add(args) -> None:
    preset = config.PROVIDERS.get(args.provider)
    if preset:
        smtp_host, smtp_port = preset["smtp_host"], preset["smtp_port"]
        smtp_ssl = bool(preset["smtp_ssl"])
        imap_host, imap_port = preset["imap_host"], preset["imap_port"]
        print(f"提示：{preset['note']}")
    else:
        smtp_host = args.smtp_host or input("SMTP 服务器地址：").strip()
        smtp_port = args.smtp_port or int(input("SMTP 端口 [465]：").strip() or 465)
        smtp_ssl = (input("SMTP 使用 SSL？(465 为是, 587/25 为否) [是]：").strip() or "是") in ("是", "y", "yes", "1") if args.smtp_ssl is None else args.smtp_ssl
        imap_host = args.imap_host or input("IMAP 服务器地址：").strip()
        imap_port = args.imap_port or int(input("IMAP 端口 [993]：").strip() or 993)

    # 非交互：命令行提供了就用，缺什么才提示
    email_addr = args.email or input("邮箱地址：").strip()
    username = args.username or (input(f"登录用户名 [{email_addr}]：").strip() or email_addr)
    password = args.password or getpass.getpass("密码/授权码（输入时不显示）：")
    default_name = email_addr.split("@")[0] if "@" in email_addr else email_addr
    name = args.name or (input(f"账号别名 [{default_name}]：").strip() or default_name)
    display_name = args.display_name if args.display_name is not None else input("发件人显示名（可留空）：").strip()

    aid = accounts.add_account(
        name=name, email=email_addr, provider=args.provider,
        username=username, password=password,
        smtp_host=smtp_host, smtp_port=smtp_port, smtp_ssl=smtp_ssl,
        imap_host=imap_host, imap_port=imap_port,
        display_name=display_name,
        daily_limit=args.daily_limit, send_interval=args.send_interval,
    )
    print(f"已添加账号 #{aid}。正在测试连接...")
    result = accounts.test_account(aid)
    if result["smtp"] and result["imap"]:
        print("SMTP 与 IMAP 连接测试通过。")
    else:
        print("连接测试未全部通过：")
        for e in result["errors"]:
            print(f"  - {e}")
        print("账号已保存，可用 account test 重新测试，或 account update 修改配置。")


def cmd_account_list(args) -> None:
    rows = accounts.list_accounts(active_only=False)
    for r in rows:
        r["smtp"] = f"{r['smtp_host']}:{r['smtp_port']}"
        r["imap"] = f"{r['imap_host']}:{r['imap_port']}"
        r["active"] = "启用" if r["active"] else "停用"
        r["today_quota"] = f"{campaigns.sent_today(r['id'])}/{r['daily_limit']}"
    print_table(rows, [
        ("id", "编号"), ("name", "别名"), ("email", "邮箱"), ("provider", "服务商"),
        ("display_name", "显示名"), ("today_quota", "今日用量"), ("active", "状态"),
    ])


def cmd_account_test(args) -> None:
    acc = pick_account(args.account_id)
    print(f"测试 {acc['email']} ...")
    result = accounts.test_account(acc["id"])
    print(f"  SMTP: {'通过' if result['smtp'] else '失败'}")
    print(f"  IMAP: {'通过' if result['imap'] else '失败'}")
    for e in result["errors"]:
        print(f"  - {e}")


def cmd_account_update(args) -> None:
    fields = {}
    for key in ("name", "display_name", "signature", "daily_limit", "send_interval",
                "smtp_host", "smtp_port", "imap_host", "imap_port"):
        val = getattr(args, key, None)
        if val is not None:
            fields[key] = val
    if args.password:
        fields["password"] = args.password
    if not fields:
        sys.exit("没有需要更新的字段")
    if accounts.update_account(args.account_id, **fields):
        print("已更新。")
    else:
        sys.exit("更新失败：账号不存在")


def cmd_account_disable(args) -> None:
    if accounts.deactivate_account(args.account_id):
        print("已停用。")
    else:
        sys.exit("账号不存在")


# ---------------------------------------------------------------------------
# profile 命令组（发件人身份）
# ---------------------------------------------------------------------------

def cmd_profile_add(args) -> None:
    acc = pick_account(args.account_id)
    signature = args.signature if args.signature is not None else input("签名（可留空）：").strip()
    pid = accounts.add_sender_profile(
        acc["id"], name=args.name, display_name=args.display_name,
        signature=signature, make_default=args.default,
    )
    print(f"已添加发件身份 #{pid}（{'默认' if args.default else '备用'}）")


def cmd_profile_list(args) -> None:
    acc = pick_account(args.account_id)
    rows = accounts.list_sender_profiles(acc["id"])
    for r in rows:
        r["is_default"] = "默认" if r["is_default"] else ""
    print_table(rows, [("id", "编号"), ("name", "场景"), ("display_name", "显示名"),
                       ("signature", "签名"), ("is_default", "默认")])


def cmd_profile_remove(args) -> None:
    if accounts.delete_sender_profile(args.profile_id):
        print("已删除。")
    else:
        sys.exit("身份不存在")


# ---------------------------------------------------------------------------
# template 命令组
# ---------------------------------------------------------------------------

def cmd_template_add(args) -> None:
    subject = args.subject if args.subject is not None else input("主题模板：").strip()
    if args.body_file:
        with open(args.body_file, "r", encoding="utf-8") as f:
            body = f.read()
    else:
        body = args.body if args.body is not None else input("正文模板：").strip()
    tid = templates.create_template(args.name, subject, body, category=args.category)
    placeholders = set()
    from .core.parser import extract_placeholders
    placeholders = extract_placeholders(subject + body)
    print(f"已创建模板 #{tid}")
    if placeholders:
        print(f"  占位符：{', '.join('{{' + p + '}}' for p in placeholders)}")


def cmd_template_list(args) -> None:
    rows = templates.list_templates(category=args.category)
    for r in rows:
        r["body"] = r["body"][:40].replace("\n", " ") + ("..." if len(r["body"]) > 40 else "")
    print_table(rows, [("id", "编号"), ("name", "名称"), ("category", "分类"),
                       ("subject", "主题"), ("body", "正文预览")])


def cmd_template_show(args) -> None:
    tpl = templates.get_template(args.template_id)
    if not tpl:
        sys.exit("模板不存在")
    print(f"名称：{tpl['name']}（{tpl['category']}）")
    print(f"主题：{tpl['subject']}")
    print("正文：")
    print(tpl["body"])


def cmd_template_preview(args) -> None:
    variables = {}
    if args.vars:
        for pair in args.vars.split(","):
            if "=" in pair:
                k, v = pair.split("=", 1)
                variables[k.strip()] = v.strip()
    result = templates.preview(args.template_id, variables)
    if not result:
        sys.exit("模板不存在")
    print("主题：", result["subject"])
    print("正文：")
    print(result["body"])
    missing = [p for p in result["placeholders"] if p not in variables]
    if missing:
        print(f"（未提供的占位符将保留原样：{', '.join(missing)}）")


def cmd_template_remove(args) -> None:
    if templates.delete_template(args.template_id):
        print("已删除。")
    else:
        sys.exit("模板不存在")


# ---------------------------------------------------------------------------
# blacklist 命令组
# ---------------------------------------------------------------------------

def cmd_blacklist_add(args) -> None:
    is_new = blacklist.add(args.email, reason=args.reason or "", source="manual")
    print("已加入黑名单。" if is_new else "该邮箱已在黑名单中，原因已更新。")


def cmd_blacklist_remove(args) -> None:
    if blacklist.remove(args.email):
        print("已移出黑名单。")
    else:
        sys.exit("该邮箱不在黑名单中")


def cmd_blacklist_list(args) -> None:
    rows = blacklist.list_blacklist()
    print_table(rows, [("email", "邮箱"), ("reason", "原因"), ("source", "来源"), ("created_at", "时间")])


# ---------------------------------------------------------------------------
# mail 命令组（普邮）
# ---------------------------------------------------------------------------

def cmd_mail_send(args) -> None:
    acc = pick_account(args.account_id)
    body = args.body
    if args.body_file:
        with open(args.body_file, "r", encoding="utf-8") as f:
            body = f.read()
    if body is None:
        body = input("正文：").strip()
    variables = None
    if args.vars:
        variables = {}
        for pair in args.vars.split(","):
            if "=" in pair:
                k, v = pair.split("=", 1)
                variables[k.strip()] = v.strip()
    try:
        result = direct_mail.send_direct(
            acc["id"], to=args.to, subject=args.subject, body=body,
            cc=args.cc, template_id=args.template_id, variables=variables,
            profile_id=args.profile_id,
        )
        print(f"已发送：{result['subject']} → {result['to']}")
    except direct_mail.DirectMailError as e:
        sys.exit(f"发送失败：{e}")


def cmd_mail_inbox(args) -> None:
    rows = direct_mail.inbox(account_id=args.account_id, unread_only=args.unread, limit=args.limit)
    for r in rows:
        r["when"] = (r["sent_at"] or r["received_at"] or "")[:16]
        r["read"] = "" if r["is_read"] else "●"
        r["body_text"] = r["body_text"][:30].replace("\n", " ")
    print_table(rows, [("id", "编号"), ("read", ""), ("when", "时间"), ("account_email", "账号"),
                       ("from_addr", "发件人"), ("subject", "主题"), ("body_text", "预览")])


def cmd_mail_read(args) -> None:
    conn = db.connect()
    try:
        row = conn.execute("SELECT * FROM messages WHERE id = ?", (args.message_id,)).fetchone()
    finally:
        conn.close()
    if not row:
        sys.exit("邮件不存在")
    direct_mail.mark_read(args.message_id)
    print(f"发件人：{row['from_addr']}")
    print(f"时间：{row['sent_at'] or row['received_at']}")
    print(f"主题：{row['subject']}")
    print("-" * 60)
    print(row["body_text"])


def cmd_mail_conversations(args) -> None:
    rows = direct_mail.conversations(account_id=args.account_id, limit=args.limit)
    for r in rows:
        r["last_time"] = (r["last_time"] or "")[:16]
    print_table(rows, [("peer", "往来对象"), ("msg_count", "邮件数"),
                       ("last_time", "最近时间"), ("last_subject", "最近主题")])


def cmd_mail_thread(args) -> None:
    rows = direct_mail.conversation_thread(args.peer, account_id=args.account_id)
    if not rows:
        print("没有与该对象的往来邮件。")
        return
    for r in rows:
        direction = "→ 发出" if r["direction"] == "out" else "← 收到"
        when = (r["sent_at"] or r["received_at"] or r["created_at"] or "")[:16]
        print(f"\n{'=' * 60}\n{direction}  {when}")
        print(f"主题：{r['subject']}")
        print(r["body_text"])


def cmd_mail_reply(args) -> None:
    acc = pick_account(args.account_id)
    body = args.body if args.body is not None else input("回复内容：").strip()
    try:
        result = direct_mail.reply(acc["id"], args.peer, body, profile_id=args.profile_id)
        print(f"已回复：{result['subject']} → {result['to']}")
    except direct_mail.DirectMailError as e:
        sys.exit(f"发送失败：{e}")


# ---------------------------------------------------------------------------
# campaign 命令组（群发）
# ---------------------------------------------------------------------------

def cmd_campaign_create(args) -> None:
    acc = pick_account(args.account_id)
    subject, body = args.subject, args.body
    if args.body_file:
        with open(args.body_file, "r", encoding="utf-8") as f:
            body = f.read()
    try:
        cid = campaigns.create_campaign(
            name=args.name, account_id=acc["id"], subject=subject or "", body=body or "",
            template_id=args.template_id, profile_id=args.profile_id,
            send_interval=args.send_interval,
        )
        print(f"已创建群发任务 #{cid}（草稿）。接下来：")
        print(f"  1. campaign import {cid} <收件人.csv>   导入收件人")
        print(f"  2. campaign start {cid}                 开始发送")
    except campaigns.CampaignError as e:
        sys.exit(f"创建失败：{e}")


def cmd_campaign_import(args) -> None:
    try:
        result = campaigns.add_recipients_from_csv(args.campaign_id, args.csv_file)
        print(f"导入完成：新增 {result['imported']} 个收件人，"
              f"黑名单拦截 {result['blocked_by_blacklist']} 个")
    except (campaigns.CampaignError, FileNotFoundError) as e:
        sys.exit(f"导入失败：{e}")


def cmd_campaign_start(args) -> None:
    try:
        campaigns.start_campaign(args.campaign_id)
        stats = campaigns.count_by_status(args.campaign_id)
        print(f"任务 #{args.campaign_id} 已开始，待发送 {stats.get('pending', 0)} 封。")
        print("发送由后台调度器执行，请运行：python -m mail_center.cli run")
    except campaigns.CampaignError as e:
        sys.exit(f"启动失败：{e}")


def cmd_campaign_pause(args) -> None:
    campaigns.pause_campaign(args.campaign_id)
    print("已暂停。用 campaign start 恢复。")


def cmd_campaign_list(args) -> None:
    rows = campaigns.list_campaigns()
    status_cn = {"draft": "草稿", "running": "发送中", "paused": "已暂停", "completed": "已完成"}
    for r in rows:
        r["status_cn"] = status_cn.get(r["status"], r["status"])
        r["progress"] = f"{r['sent_cnt'] + r['replied_cnt'] + r['bounced_cnt']}/{r['total']}"
    print_table(rows, [("id", "编号"), ("name", "名称"), ("account_email", "发件账号"),
                       ("status_cn", "状态"), ("progress", "进度"),
                       ("replied_cnt", "回复"), ("bounced_cnt", "退信")])


def cmd_campaign_stats(args) -> None:
    if args.campaign_id:
        s = tracking.campaign_stats(args.campaign_id)
        _print_stats(s)
    else:
        merged = tracking.merged_stats([c["id"] for c in campaigns.list_campaigns()])
        _print_merged(merged)


def _print_stats(s: dict) -> None:
    print(f"任务 #{s['campaign_id']}：{s['name']}（{s['status']}）")
    print(f"  总收件人：{s['total']}")
    print(f"  已发送：  {s['sent']}（含回复与退信）")
    print(f"  已回复：  {s['replied']}（回复率 {s['reply_rate']}%）")
    print(f"  退信：    {s['bounced']}")
    print(f"  待发送：  {s['pending']}")
    print(f"  黑名单拦截：{s['skipped_blacklist']}")


def _print_merged(m: dict) -> None:
    print(f"合并统计（{m['campaign_count']} 个任务）")
    print(f"  总收件人：{m['total']}")
    print(f"  已发送：  {m['sent']}")
    print(f"  已回复：  {m['replied']}（回复率 {m['reply_rate']}%）")
    print(f"  退信：    {m['bounced']}")
    print(f"  待发送：  {m['pending']}")
    print(f"  黑名单拦截：{m['skipped_blacklist']}")


# ---------------------------------------------------------------------------
# track 命令组
# ---------------------------------------------------------------------------

def cmd_track_merge(args) -> None:
    merged = tracking.merged_stats(args.campaign_ids)
    _print_merged(merged)


def cmd_track_unreplied(args) -> None:
    rows = tracking.unreplied_list(campaign_id=args.campaign_id, limit=args.limit)
    for r in rows:
        r["sent_at"] = (r["sent_at"] or "")[:16]
    print_table(rows, [("email", "邮箱"), ("display_name", "姓名"), ("company", "公司"),
                       ("sent_at", "发送时间"), ("campaign_name", "任务")])
    print(f"\n共 {len(rows)} 人未回复")


def cmd_track_bounced(args) -> None:
    rows = tracking.bounced_list(campaign_id=args.campaign_id, limit=args.limit)
    print_table(rows, [("email", "邮箱"), ("bounce_reason", "原因"), ("campaign_name", "任务")])


def cmd_track_replied(args) -> None:
    rows = tracking.replied_list(campaign_id=args.campaign_id, limit=args.limit)
    for r in rows:
        r["replied_at"] = (r["replied_at"] or "")[:16]
        r["reply_excerpt"] = (r["reply_excerpt"] or "").replace("\n", " ")
    print_table(rows, [("email", "邮箱"), ("replied_at", "回复时间"),
                       ("campaign_name", "任务"), ("reply_excerpt", "回复摘要")])


def cmd_track_export_unreplied(args) -> None:
    path = tracking.export_unreplied_csv(campaign_id=args.campaign_id, path=args.output)
    print(f"已导出：{path}")


# ---------------------------------------------------------------------------
# leads 命令组
# ---------------------------------------------------------------------------

def cmd_leads_list(args) -> None:
    rows = tracking.list_leads(level=args.level, min_score=args.min_score, limit=args.limit)
    for r in rows:
        r["latest_reply_at"] = (r["latest_reply_at"] or "")[:16]
    print_table(rows, [("email", "邮箱"), ("name", "姓名"), ("intent_label", "意向"),
                       ("intent_score", "分数"), ("reply_count", "回复数"),
                       ("latest_reply_at", "最近回复")])
    high = sum(1 for r in rows if r["intent_level"] == "high")
    print(f"\n共 {len(rows)} 位，其中高意向 {high} 位")


def cmd_leads_set(args) -> None:
    if tracking.set_lead_level(args.email, args.level):
        print(f"已将 {args.email} 标记为「{LEVEL_LABELS[args.level]}」意向")
    else:
        sys.exit("客户不存在或等级无效（可选 high/medium/low/none）")


def cmd_leads_export(args) -> None:
    path = tracking.export_leads_csv(path=args.output, level=args.level, min_score=args.min_score)
    print(f"已导出：{path}")


# ---------------------------------------------------------------------------
# poll / run
# ---------------------------------------------------------------------------

def cmd_poll(args) -> None:
    summary = inbox.poll_all_accounts()
    print(f"轮询完成：新邮件 {summary['new']}，回复 {summary['replies']}，退信 {summary['bounces']}")
    for e in summary["errors"]:
        print(f"  错误：{e}")


def cmd_sync(args) -> None:
    acc = pick_account(args.account_id)
    print(f"同步 {acc['email']} 最近 {args.limit} 封邮件（含已读）...")
    result = inbox.sync_account(acc["id"], limit=args.limit)
    print(f"同步完成：新入库 {result['new']}，回填图片 {result.get('backfilled', 0)}，"
          f"回复 {result['replies']}，退信 {result['bounces']}")


def cmd_run(args) -> None:
    print("启动邮件中心调度器（Ctrl+C 停止）...")
    print(f"  收件箱轮询间隔：{config.INBOX_POLL_INTERVAL}s")
    scheduler.start(foreground=True)


def cmd_web(args) -> None:
    from . import web
    web.run(host=args.host, port=args.port, start_scheduler=not args.no_scheduler)


# ---------------------------------------------------------------------------
# 参数解析
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="mailcenter", description="邮件中心系统")
    sub = p.add_subparsers(dest="command", required=True)

    # account
    pa = sub.add_parser("account", help="邮箱管理")
    pas = pa.add_subparsers(dest="action", required=True)

    x = pas.add_parser("add", help="绑定邮箱")
    x.add_argument("--provider", default="custom",
                   choices=list(config.PROVIDERS.keys()) + ["custom"],
                   help="邮箱服务商（自动填充服务器配置）")
    x.add_argument("--email", help="邮箱地址（提供后免交互）")
    x.add_argument("--username", help="登录用户名（默认同邮箱）")
    x.add_argument("--password", help="密码/授权码（提供后免交互；建议用后在服务商处重新生成）")
    x.add_argument("--name", help="账号别名")
    x.add_argument("--display-name", dest="display_name", help="发件人显示名")
    x.add_argument("--smtp-host", dest="smtp_host", help="SMTP 服务器（custom 时用）")
    x.add_argument("--smtp-port", dest="smtp_port", type=int, help="SMTP 端口")
    x.add_argument("--smtp-ssl", dest="smtp_ssl", type=int, choices=[0, 1], default=None, help="SMTP 是否 SSL")
    x.add_argument("--imap-host", dest="imap_host", help="IMAP 服务器（custom 时用）")
    x.add_argument("--imap-port", dest="imap_port", type=int, help="IMAP 端口")
    x.add_argument("--daily-limit", type=int, default=300, help="每日发送上限（默认 300）")
    x.add_argument("--send-interval", type=int, default=30, help="群发间隔秒数（默认 30）")
    x.set_defaults(func=cmd_account_add)

    x = pas.add_parser("list", help="列出账号")
    x.set_defaults(func=cmd_account_list)

    x = pas.add_parser("test", help="测试连接")
    x.add_argument("--account-id", type=int)
    x.set_defaults(func=cmd_account_test)

    x = pas.add_parser("update", help="更新配置")
    x.add_argument("account_id", type=int)
    x.add_argument("--name"); x.add_argument("--display-name", dest="display_name")
    x.add_argument("--signature"); x.add_argument("--password")
    x.add_argument("--daily-limit", dest="daily_limit", type=int)
    x.add_argument("--send-interval", dest="send_interval", type=int)
    x.add_argument("--smtp-host", dest="smtp_host"); x.add_argument("--smtp-port", dest="smtp_port", type=int)
    x.add_argument("--imap-host", dest="imap_host"); x.add_argument("--imap-port", dest="imap_port", type=int)
    x.set_defaults(func=cmd_account_update)

    x = pas.add_parser("disable", help="停用账号")
    x.add_argument("account_id", type=int)
    x.set_defaults(func=cmd_account_disable)

    # profile
    pp = sub.add_parser("profile", help="发件人身份")
    pps = pp.add_subparsers(dest="action", required=True)

    x = pps.add_parser("add", help="添加身份")
    x.add_argument("name", help="场景名，如 商务/售后")
    x.add_argument("display_name", help="显示名")
    x.add_argument("--account-id", type=int)
    x.add_argument("--signature", help="签名")
    x.add_argument("--default", action="store_true", help="设为默认身份")
    x.set_defaults(func=cmd_profile_add)

    x = pps.add_parser("list", help="列出身份")
    x.add_argument("--account-id", type=int)
    x.set_defaults(func=cmd_profile_list)

    x = pps.add_parser("remove", help="删除身份")
    x.add_argument("profile_id", type=int)
    x.set_defaults(func=cmd_profile_remove)

    # template
    pt = sub.add_parser("template", help="模板库")
    pts = pt.add_subparsers(dest="action", required=True)

    x = pts.add_parser("add", help="创建模板")
    x.add_argument("name")
    x.add_argument("--category", default="通用")
    x.add_argument("--subject", help="主题模板")
    x.add_argument("--body", help="正文模板")
    x.add_argument("--body-file", help="从文件读取正文模板")
    x.set_defaults(func=cmd_template_add)

    x = pts.add_parser("list", help="列出模板")
    x.add_argument("--category")
    x.set_defaults(func=cmd_template_list)

    x = pts.add_parser("show", help="查看模板")
    x.add_argument("template_id", type=int)
    x.set_defaults(func=cmd_template_show)

    x = pts.add_parser("preview", help="预览变量替换效果")
    x.add_argument("template_id", type=int)
    x.add_argument("--vars", help="变量，格式 name=张三,company=某公司")
    x.set_defaults(func=cmd_template_preview)

    x = pts.add_parser("remove", help="删除模板")
    x.add_argument("template_id", type=int)
    x.set_defaults(func=cmd_template_remove)

    # blacklist
    pb = sub.add_parser("blacklist", help="黑名单")
    pbs = pb.add_subparsers(dest="action", required=True)

    x = pbs.add_parser("add", help="加入黑名单")
    x.add_argument("email")
    x.add_argument("--reason", help="原因")
    x.set_defaults(func=cmd_blacklist_add)

    x = pbs.add_parser("remove", help="移出黑名单")
    x.add_argument("email")
    x.set_defaults(func=cmd_blacklist_remove)

    x = pbs.add_parser("list", help="查看黑名单")
    x.set_defaults(func=cmd_blacklist_list)

    # mail
    pm = sub.add_parser("mail", help="普邮管理")
    pms = pm.add_subparsers(dest="action", required=True)

    x = pms.add_parser("send", help="点对点发送")
    x.add_argument("--to", required=True)
    x.add_argument("--subject", required=True)
    x.add_argument("--body")
    x.add_argument("--body-file")
    x.add_argument("--cc")
    x.add_argument("--account-id", type=int)
    x.add_argument("--profile-id", type=int)
    x.add_argument("--template-id", type=int)
    x.add_argument("--vars")
    x.set_defaults(func=cmd_mail_send)

    x = pms.add_parser("inbox", help="统一收件箱")
    x.add_argument("--account-id", type=int)
    x.add_argument("--unread", action="store_true")
    x.add_argument("--limit", type=int, default=50)
    x.set_defaults(func=cmd_mail_inbox)

    x = pms.add_parser("read", help="阅读邮件")
    x.add_argument("message_id", type=int)
    x.set_defaults(func=cmd_mail_read)

    x = pms.add_parser("conversations", help="会话列表")
    x.add_argument("--account-id", type=int)
    x.add_argument("--limit", type=int, default=50)
    x.set_defaults(func=cmd_mail_conversations)

    x = pms.add_parser("thread", help="查看会话")
    x.add_argument("peer", help="往来对象邮箱")
    x.add_argument("--account-id", type=int)
    x.set_defaults(func=cmd_mail_thread)

    x = pms.add_parser("reply", help="回复会话")
    x.add_argument("peer")
    x.add_argument("--body")
    x.add_argument("--account-id", type=int)
    x.add_argument("--profile-id", type=int)
    x.set_defaults(func=cmd_mail_reply)

    # campaign
    pc = sub.add_parser("campaign", help="群发任务")
    pcs = pc.add_subparsers(dest="action", required=True)

    x = pcs.add_parser("create", help="创建任务")
    x.add_argument("name")
    x.add_argument("--account-id", type=int)
    x.add_argument("--subject")
    x.add_argument("--body")
    x.add_argument("--body-file")
    x.add_argument("--template-id", type=int)
    x.add_argument("--profile-id", type=int)
    x.add_argument("--send-interval", type=int)
    x.set_defaults(func=cmd_campaign_create)

    x = pcs.add_parser("import", help="导入收件人 CSV")
    x.add_argument("campaign_id", type=int)
    x.add_argument("csv_file")
    x.set_defaults(func=cmd_campaign_import)

    x = pcs.add_parser("start", help="开始发送")
    x.add_argument("campaign_id", type=int)
    x.set_defaults(func=cmd_campaign_start)

    x = pcs.add_parser("pause", help="暂停")
    x.add_argument("campaign_id", type=int)
    x.set_defaults(func=cmd_campaign_pause)

    x = pcs.add_parser("list", help="任务列表")
    x.set_defaults(func=cmd_campaign_list)

    x = pcs.add_parser("stats", help="任务统计（不传编号则合并全部）")
    x.add_argument("campaign_id", type=int, nargs="?")
    x.set_defaults(func=cmd_campaign_stats)

    # track
    pk = sub.add_parser("track", help="追踪统计")
    pks = pk.add_subparsers(dest="action", required=True)

    x = pks.add_parser("merge", help="合并多个任务统计")
    x.add_argument("campaign_ids", type=int, nargs="+")
    x.set_defaults(func=cmd_track_merge)

    x = pks.add_parser("unreplied", help="未回复清单")
    x.add_argument("--campaign-id", type=int)
    x.add_argument("--limit", type=int, default=100)
    x.set_defaults(func=cmd_track_unreplied)

    x = pks.add_parser("bounced", help="退信清单")
    x.add_argument("--campaign-id", type=int)
    x.add_argument("--limit", type=int, default=100)
    x.set_defaults(func=cmd_track_bounced)

    x = pks.add_parser("replied", help="回复清单")
    x.add_argument("--campaign-id", type=int)
    x.add_argument("--limit", type=int, default=100)
    x.set_defaults(func=cmd_track_replied)

    x = pks.add_parser("export-unreplied", help="导出未回复清单 CSV")
    x.add_argument("--campaign-id", type=int)
    x.add_argument("--output")
    x.set_defaults(func=cmd_track_export_unreplied)

    # leads
    pl = sub.add_parser("leads", help="意向客户")
    pls = pl.add_subparsers(dest="action", required=True)

    x = pls.add_parser("list", help="意向客户列表")
    x.add_argument("--level", choices=["high", "medium", "low", "none"])
    x.add_argument("--min-score", type=int)
    x.add_argument("--limit", type=int, default=100)
    x.set_defaults(func=cmd_leads_list)

    x = pls.add_parser("set", help="手动调整意向等级")
    x.add_argument("email")
    x.add_argument("level", choices=["high", "medium", "low", "none"])
    x.set_defaults(func=cmd_leads_set)

    x = pls.add_parser("export", help="导出意向客户 CSV")
    x.add_argument("--level", choices=["high", "medium", "low", "none"])
    x.add_argument("--min-score", type=int)
    x.add_argument("--output")
    x.set_defaults(func=cmd_leads_export)

    # poll / run
    x = sub.add_parser("poll", help="立即轮询一次收件箱")
    x.set_defaults(func=cmd_poll)

    x = sub.add_parser("sync", help="全量同步账号最近邮件（含已读，用于初始化/恢复）")
    x.add_argument("--account-id", type=int)
    x.add_argument("--limit", type=int, default=200)
    x.set_defaults(func=cmd_sync)

    x = sub.add_parser("web", help="启动 Web 界面与后台调度器")
    x.add_argument("--host", default="127.0.0.1")
    x.add_argument("--port", type=int, default=8000)
    x.add_argument("--no-scheduler", action="store_true", help="不启动后台调度")
    x.set_defaults(func=cmd_web)

    x = sub.add_parser("run", help="启动后台调度器（前台运行）")
    x.set_defaults(func=cmd_run)

    return p


def main() -> None:
    db.init_db()
    parser = build_parser()
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
