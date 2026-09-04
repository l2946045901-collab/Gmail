"""Flask Web 服务：邮件中心系统的 HTTP API + 前端托管。

启动：python -m mail_center.web
默认 http://127.0.0.1:8000
后台调度器随服务启动（收件箱轮询 + 群发发送队列常驻）。
"""

from pathlib import Path

from flask import Flask, jsonify, request, send_from_directory

from .. import db
from ..services import (accounts, blacklist, campaigns, direct_mail, inbox, templates, tracking)
from ..services import attachments as attachments_svc

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"


def create_app() -> Flask:
    app = Flask(__name__, static_folder=str(STATIC_DIR), static_url_path="/static")
    app.json.ensure_ascii = False

    # ---------------------------------------------------------------- 前端
    @app.route("/")
    def index():
        return send_from_directory(STATIC_DIR, "index.html")

    # ---------------------------------------------------------------- 工具
    def _err(msg: str, code: int = 400):
        return jsonify({"ok": False, "error": msg}), code

    def _account_fields(body: dict) -> dict:
        """从请求体提取账号字段，做别名兼容。"""
        mapping = {
            "name": "name", "email": "email", "provider": "provider",
            "username": "username", "password": "password",
            "smtp_host": "smtp_host", "smtp_port": "smtp_port",
            "imap_host": "imap_host", "imap_port": "imap_port",
            "display_name": "display_name", "signature": "signature",
            "daily_limit": "daily_limit", "send_interval": "send_interval",
        }
        return {db_key: body[k] for k, db_key in mapping.items() if k in body}

    # ---------------------------------------------------------------- 账号
    @app.route("/api/providers")
    def api_providers():
        from .. import config
        return jsonify(config.PROVIDERS)

    @app.route("/api/accounts", methods=["GET"])
    def api_accounts_list():
        rows = accounts.list_accounts(active_only=False)
        for r in rows:
            r["today_sent"] = campaigns.sent_today(r["id"])
            r.pop("password_enc", None)
        return jsonify(rows)

    @app.route("/api/accounts", methods=["POST"])
    def api_accounts_add():
        body = request.get_json(force=True)
        from .. import config
        prov = body.get("provider", "custom")
        preset = config.PROVIDERS.get(prov, {})
        required = ["name", "email", "username", "password", "smtp_host", "imap_host"]
        # 服务商预置会自动补 host，custom 需前端填全
        for k in ("name", "email", "username", "password"):
            if not body.get(k):
                return _err(f"缺少字段：{k}")
        smtp_host = body.get("smtp_host") or preset.get("smtp_host", "")
        imap_host = body.get("imap_host") or preset.get("imap_host", "")
        if not smtp_host or not imap_host:
            return _err("custom 服务商需填写 smtp_host 和 imap_host")
        aid = accounts.add_account(
            name=body["name"], email=body["email"], provider=prov,
            username=body["username"], password=body["password"],
            smtp_host=smtp_host, smtp_port=int(body.get("smtp_port") or preset.get("smtp_port", 465)),
            smtp_ssl=bool(body.get("smtp_ssl") if "smtp_ssl" in body else preset.get("smtp_ssl", 1)),
            imap_host=imap_host, imap_port=int(body.get("imap_port") or preset.get("imap_port", 993)),
            display_name=body.get("display_name", ""), signature=body.get("signature", ""),
            daily_limit=int(body.get("daily_limit", 300)), send_interval=int(body.get("send_interval", 30)),
        )
        result = accounts.test_account(aid)
        return jsonify({"ok": True, "id": aid, "test": result})

    @app.route("/api/accounts/<int:aid>/test", methods=["POST"])
    def api_accounts_test(aid):
        return jsonify(accounts.test_account(aid))

    @app.route("/api/accounts/<int:aid>", methods=["PUT"])
    def api_accounts_update(aid):
        body = _account_fields(request.get_json(force=True))
        if not body:
            return _err("没有需要更新的字段")
        return jsonify({"ok": accounts.update_account(aid, **body)})

    @app.route("/api/accounts/<int:aid>", methods=["DELETE"])
    def api_accounts_delete(aid):
        return jsonify({"ok": accounts.deactivate_account(aid)})

    # ---------------------------------------------------------------- 身份
    @app.route("/api/accounts/<int:aid>/profiles")
    def api_profiles(aid):
        return jsonify(accounts.list_sender_profiles(aid))

    @app.route("/api/accounts/<int:aid>/profiles", methods=["POST"])
    def api_profile_add(aid):
        body = request.get_json(force=True)
        pid = accounts.add_sender_profile(
            aid, body["name"], body["display_name"],
            body.get("signature", ""), bool(body.get("is_default")))
        return jsonify({"ok": True, "id": pid})

    @app.route("/api/profiles/<int:pid>", methods=["DELETE"])
    def api_profile_del(pid):
        return jsonify({"ok": accounts.delete_sender_profile(pid)})

    # ---------------------------------------------------------------- 邮件
    @app.route("/api/inbox")
    def api_inbox():
        account_id = request.args.get("account_id", type=int)
        unread = request.args.get("unread") == "1"
        rows = direct_mail.inbox(account_id=account_id, unread_only=unread)
        return jsonify(rows)

    @app.route("/api/messages/<int:mid>")
    def api_message(mid):
        conn = db.connect()
        try:
            row = conn.execute("SELECT * FROM messages WHERE id = ?", (mid,)).fetchone()
        finally:
            conn.close()
        if not row:
            return _err("邮件不存在", 404)
        data = dict(row)
        data["has_html"] = bool(data.get("body_html"))
        data.pop("body_html", None)  # 正文通过 /html 端点渲染，列表接口不带大字段
        return jsonify(data)

    @app.route("/api/messages/<int:mid>/html")
    def api_message_html(mid):
        """返回可安全渲染的邮件 HTML：cid 图片本地化，远程图片默认按需加载。

        ?remote=1 时保留远程图片直链（用户点击"显示图片"后重新拉取）。
        """
        import re
        conn = db.connect()
        try:
            row = conn.execute(
                "SELECT body_html, body_text, attachment_dir FROM messages WHERE id = ?", (mid,)
            ).fetchone()
        finally:
            conn.close()
        if not row:
            return _err("邮件不存在", 404)
        html = row["body_html"] or ""
        if not html:
            return jsonify({"html": "", "remote_count": 0})

        cid_map = attachments_svc.load_cid_map(mid)
        allow_remote = request.args.get("remote") == "1"
        remote_count = 0

        # 1. cid: 引用 → 本地附件 URL
        def _cid_repl(m):
            attr, q, cid = m.group(1), m.group(2), m.group(3)[4:].strip()  # 去掉前缀 cid:
            fname = cid_map.get(cid) or cid_map.get(cid.strip("<>"))
            if fname:
                return f"{attr}={q}/api/attach/{mid}/{fname}{q}"
            return m.group(0)
        html = re.sub(r'(src)\s*=\s*(["\'])(cid:[^"\']+)\2', _cid_repl, html, flags=re.I)
        # 无引号的裸 cid 引用兜底
        for cid, fname in cid_map.items():
            if cid:
                html = html.replace(f"cid:{cid}", f"/api/attach/{mid}/{fname}")

        # 2. 远程 http(s) 图片：默认换成占位并记录 data-src（点击"显示图片"后经 ?remote=1 恢复）
        if not allow_remote:
            _ph = "data:image/gif;base64,R0lGODlhAQABAAAAACH5BAEKAAEALAAAAAABAAEAAAICTAEAOw=="
            def _img_sub(m):
                nonlocal remote_count
                remote_count += 1
                return f'<img{m.group(1)}src="{_ph}" data-src="{m.group(3)}"'
            html = re.sub(r'<img([^>]*?)src=(["\'])(https?:[^"\']+)\2', _img_sub, html, flags=re.I)

        # 3. 剥离危险内容（前端 iframe sandbox 是第一道防线，这里双保险）
        html = re.sub(r"<script[^>]*>.*?</script\s*>", "", html, flags=re.S | re.I)
        html = re.sub(r"<(iframe|object|embed|form)[^>]*>", "<span>", html, flags=re.I)
        html = re.sub(r"\son\w+\s*=\s*(\"[^\"]*\"|'[^']*'|[^\s>]+)", "", html, flags=re.I)

        wrap_css = ("<base target='_blank'><style>body{font-family:'Microsoft YaHei',"
                    "'PingFang SC',sans-serif;margin:10px;color:#1E232B;word-break:break-word}"
                    "img{max-width:100%;height:auto}</style>")
        # 插入到 head 或最前
        if re.search(r"<head[^>]*>", html, flags=re.I):
            html = re.sub(r"(<head[^>]*>)", r"\1" + wrap_css, html, count=1, flags=re.I)
        else:
            html = wrap_css + html
        return jsonify({"html": html, "remote_count": remote_count})

    @app.route("/api/attach/<int:mid>/<path:fname>")
    def api_attachment(mid, fname):
        from flask import send_file
        path = attachments_svc.resolve(mid, fname)
        if not path:
            return _err("附件不存在", 404)
        import mimetypes
        ctype = mimetypes.guess_type(fname)[0] or "application/octet-stream"
        # svg 可能携带脚本，强制以纯下载方式提供
        safe = not fname.endswith(".svg")
        return send_file(path, mimetype=ctype, as_attachment=not safe)

    @app.route("/api/messages/<int:mid>/read", methods=["POST"])
    def api_message_read(mid):
        return jsonify({"ok": direct_mail.mark_read(mid)})

    @app.route("/api/conversations")
    def api_conversations():
        account_id = request.args.get("account_id", type=int)
        return jsonify(direct_mail.conversations(account_id=account_id))

    @app.route("/api/conversations/thread")
    def api_thread():
        peer = request.args.get("peer", "")
        account_id = request.args.get("account_id", type=int)
        return jsonify(direct_mail.conversation_thread(peer, account_id))

    @app.route("/api/send", methods=["POST"])
    def api_send():
        body = request.get_json(force=True)
        try:
            result = direct_mail.send_direct(
                account_id=int(body["account_id"]), to=body["to"],
                subject=body["subject"], body=body.get("body", ""),
                cc=body.get("cc"), template_id=body.get("template_id"),
                variables=body.get("variables"), profile_id=body.get("profile_id"),
            )
            return jsonify(result)
        except direct_mail.DirectMailError as e:
            return _err(str(e))

    # ---------------------------------------------------------------- 轮询
    @app.route("/api/poll", methods=["POST"])
    def api_poll():
        return jsonify(inbox.poll_all_accounts())

    # ---------------------------------------------------------------- 模板
    @app.route("/api/templates")
    def api_templates():
        return jsonify(templates.list_templates(category=request.args.get("category")))

    @app.route("/api/templates", methods=["POST"])
    def api_template_add():
        b = request.get_json(force=True)
        tid = templates.create_template(b["name"], b["subject"], b["body"], b.get("category", "通用"))
        return jsonify({"ok": True, "id": tid})

    @app.route("/api/templates/<int:tid>", methods=["DELETE"])
    def api_template_del(tid):
        return jsonify({"ok": templates.delete_template(tid)})

    @app.route("/api/templates/<int:tid>/preview", methods=["POST"])
    def api_template_preview(tid):
        return jsonify(templates.preview(tid, request.get_json(force=True).get("variables", {})))

    # ---------------------------------------------------------------- 黑名单
    @app.route("/api/blacklist")
    def api_blacklist():
        return jsonify(blacklist.list_blacklist())

    @app.route("/api/blacklist", methods=["POST"])
    def api_blacklist_add():
        b = request.get_json(force=True)
        return jsonify({"ok": blacklist.add(b["email"], b.get("reason", ""), "manual")})

    @app.route("/api/blacklist/<path:email>", methods=["DELETE"])
    def api_blacklist_del(email):
        return jsonify({"ok": blacklist.remove(email)})

    # ---------------------------------------------------------------- 群发
    @app.route("/api/campaigns")
    def api_campaigns():
        return jsonify(campaigns.list_campaigns())

    @app.route("/api/campaigns", methods=["POST"])
    def api_campaign_create():
        b = request.get_json(force=True)
        try:
            cid = campaigns.create_campaign(
                name=b["name"], account_id=int(b["account_id"]),
                subject=b.get("subject", ""), body=b.get("body", ""),
                template_id=b.get("template_id"), profile_id=b.get("profile_id"),
                send_interval=b.get("send_interval"))
            # 收件人内联提交（数组，含 email/name/company/其他变量列）
            recips = b.get("recipients")
            inline_added = 0
            if recips:
                lines = ["email,name,company"] + [
                    ",".join([str(r.get("email", "")), r.get("name", ""), r.get("company", "")])
                    for r in recips
                ]
                res = campaigns.add_recipients_from_text(cid, "\n".join(lines))
                inline_added = res["imported"]
            return jsonify({"ok": True, "id": cid, "imported": inline_added})
        except campaigns.CampaignError as e:
            return _err(str(e))

    @app.route("/api/campaigns/<int:cid>/recipients", methods=["POST"])
    def api_campaign_import(cid):
        """粘贴 CSV 文本导入收件人。"""
        text = request.get_json(force=True).get("csv", "")
        try:
            return jsonify(campaigns.add_recipients_from_text(cid, text))
        except campaigns.CampaignError as e:
            return _err(str(e))

    @app.route("/api/campaigns/<int:cid>/start", methods=["POST"])
    def api_campaign_start(cid):
        try:
            campaigns.start_campaign(cid)
            return jsonify({"ok": True})
        except campaigns.CampaignError as e:
            return _err(str(e))

    @app.route("/api/campaigns/<int:cid>/pause", methods=["POST"])
    def api_campaign_pause(cid):
        campaigns.pause_campaign(cid)
        return jsonify({"ok": True})

    @app.route("/api/campaigns/<int:cid>/stats")
    def api_campaign_stats(cid):
        return jsonify(tracking.campaign_stats(cid))

    @app.route("/api/campaigns/<int:cid>/recipients")
    def api_campaign_recipients(cid):
        conn = db.connect()
        try:
            rows = conn.execute(
                "SELECT email, display_name, company, status, sent_at, replied_at, bounce_reason, skipped_reason "
                "FROM recipients WHERE campaign_id = ? ORDER BY id", (cid,)).fetchall()
            return jsonify([dict(r) for r in rows])
        finally:
            conn.close()

    # ---------------------------------------------------------------- 追踪
    @app.route("/api/track/merge")
    def api_merge():
        ids = request.args.getlist("id", type=int)
        if not ids:
            ids = [c["id"] for c in campaigns.list_campaigns()]
        return jsonify(tracking.merged_stats(ids))

    @app.route("/api/track/unreplied")
    def api_unreplied():
        return jsonify(tracking.unreplied_list(request.args.get("campaign_id", type=int)))

    @app.route("/api/track/replied")
    def api_replied():
        return jsonify(tracking.replied_list(request.args.get("campaign_id", type=int)))

    @app.route("/api/track/bounced")
    def api_bounced():
        return jsonify(tracking.bounced_list(request.args.get("campaign_id", type=int)))

    # ---------------------------------------------------------------- 意向
    @app.route("/api/leads")
    def api_leads():
        return jsonify(tracking.list_leads(
            level=request.args.get("level"),
            min_score=request.args.get("min_score", type=int)))

    @app.route("/api/leads/<path:email>/level", methods=["PUT"])
    def api_lead_level(email):
        level = request.get_json(force=True).get("level")
        return jsonify({"ok": tracking.set_lead_level(email, level)})

    @app.route("/api/leads/<path:email>/blacklist", methods=["POST"])
    def api_lead_blacklist(email):
        tracking.set_lead_level(email, "none")
        return jsonify({"ok": blacklist.add(email, "意向客户标记为已拒绝", "rejected")})

    @app.route("/api/leads/export")
    def api_leads_export():
        import io
        from flask import Response
        level = request.args.get("level")
        leads = tracking.list_leads(level=level, limit=100000)
        import csv as _csv
        buf = io.StringIO()
        w = _csv.writer(buf)
        w.writerow(["邮箱", "姓名", "公司", "意向等级", "意向分数", "回复次数", "最近回复时间"])
        for l in leads:
            w.writerow([l["email"], l["name"], l["company"], l["intent_label"],
                        l["intent_score"], l["reply_count"], l["latest_reply_at"] or ""])
        csv_bytes = ("\ufeff" + buf.getvalue()).encode("utf-8")  # BOM 让 Excel 正确识别 UTF-8
        fname = f"leads_{'high' if level else 'all'}.csv"
        return Response(csv_bytes, mimetype="text/csv",
                        headers={"Content-Disposition": f"attachment; filename={fname}"})

    # ---------------------------------------------------------------- 概览
    @app.route("/api/dashboard")
    def api_dashboard():
        accs = accounts.list_accounts(active_only=True)
        total_sent = sum(campaigns.sent_today(a["id"]) for a in accs)
        unread = sum(1 for _ in direct_mail.inbox(unread_only=True))
        camps = campaigns.list_campaigns()
        leads = tracking.list_leads(level="high")
        return jsonify({
            "accounts": len(accs),
            "sent_today": total_sent,
            "daily_cap": sum(a["daily_limit"] for a in accs),
            "unread": unread,
            "campaigns_running": sum(1 for c in camps if c["status"] == "running"),
            "campaigns_total": len(camps),
            "high_intent": len(leads),
        })

    return app
