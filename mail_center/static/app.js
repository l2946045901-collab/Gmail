/* 邮件中心运营台 — 前端逻辑（原生 JS，无构建） */

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));

const TITLES = {
  dashboard: "概览", inbox: "收件箱", conversations: "会话", compose: "写邮件",
  campaigns: "群发任务", tracking: "追踪统计", leads: "意向客户",
  templates: "模板库", blacklist: "黑名单", accounts: "邮箱管理",
};

// ------------------------------------------------------------------ 工具
async function api(path, opts = {}) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...opts,
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || `请求失败 ${res.status}`);
  return data;
}

function esc(s) {
  return (s == null ? "" : String(s)).replace(/[&<>"]/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
}
function fmtTime(s) { return s ? s.replace("T", " ").slice(0, 16) : "—"; }
function trunc(s, n) { s = String(s || ""); return s.length > n ? s.slice(0, n) + "…" : s; }

function toast(msg, isError) {
  const t = $("#toast");
  t.textContent = msg;
  t.className = "toast show" + (isError ? " error" : "");
  clearTimeout(t._timer);
  t._timer = setTimeout(() => (t.className = "toast hidden"), isError ? 5000 : 2600);
}

function openModal(title, bodyHtml) {
  $("#modal-title").textContent = title;
  $("#modal-body").innerHTML = bodyHtml;
  $("#modal").classList.remove("hidden");
}
function closeModal() { $("#modal").classList.add("hidden"); }

let ACCOUNTS = [];

// ------------------------------------------------------------------ 路由
function showView(name) {
  $$(".nav-item").forEach((b) => b.classList.toggle("active", b.dataset.view === name));
  $$(".view").forEach((v) => v.classList.add("hidden"));
  $("#v-" + name).classList.remove("hidden");
  $("#view-title").textContent = TITLES[name];
  (VIEWS[name] || (() => {}))();
}

// ------------------------------------------------------------------ 各视图
const VIEWS = {
  async dashboard() {
    const d = await api("/api/dashboard");
    const camps = await api("/api/campaigns");
    const pct = d.daily_cap ? Math.round((d.sent_today / d.daily_cap) * 100) : 0;
    $("#v-dashboard").innerHTML = `
      <div class="metrics">
        <div class="metric-card"><div class="metric-label">今日发送 / 上限</div>
          <div class="metric-value num">${d.sent_today}<span style="font-size:16px;color:var(--postmark-light)">/${d.daily_cap}</span></div>
          <div class="quota-bar"><div class="quota-fill" style="width:${pct}%"></div></div></div>
        <div class="metric-card"><div class="metric-label">未读邮件</div>
          <div class="metric-value num">${d.unread}</div>
          <div class="metric-note">统一收件箱</div></div>
        <div class="metric-card"><div class="metric-label">高意向客户</div>
          <div class="metric-value num accent">${d.high_intent}</div>
          <div class="metric-note">需优先跟进</div></div>
        <div class="metric-card"><div class="metric-label">进行中的群发</div>
          <div class="metric-value num">${d.campaigns_running}</div>
          <div class="metric-note">共 ${d.campaigns_total} 个任务</div></div>
      </div>
      <div class="panel">
        <div class="panel-head"><h3>最近群发任务</h3>
          <button class="btn small" onclick="showView('campaigns')">查看全部</button></div>
        <div class="panel-body">${camps.length ? campTable(camps) :
          '<div class="empty"><strong>还没有群发任务</strong>创建一个任务，导入客户名单开始触达</div>'}</div>
      </div>`;
  },

  async inbox() {
    fillAccountSelect("#inbox-account");
    const unread = $("#inbox-unread").checked;
    const acc = $("#inbox-account").value;
    const rows = await api(`/api/inbox?unread=${unread ? 1 : 0}${acc ? "&account_id=" + acc : ""}`);
    $("#inbox-list").innerHTML = rows.length ? `
      <div class="panel"><table><tbody>
        ${rows.map((m) => `
          <tr class="clickable ${m.is_read ? "" : "unread"}" onclick="readMessage(${m.id})">
            <td style="width:210px;color:var(--postmark)" class="num">${fmtTime(m.sent_at || m.received_at)}</td>
            <td style="width:200px">${esc(trunc(m.from_addr, 26))}</td>
            <td class="msg-subject">${esc(trunc(m.subject, 60)) || "(无主题)"}</td>
            <td style="width:120px;color:var(--postmark-light);font-size:12px">${esc(m.account_email)}</td>
          </tr>`).join("")}
      </tbody></table></div>` :
      '<div class="empty"><strong>收件箱为空</strong>点击"刷新收件箱"拉取新邮件</div>';
  },

  async conversations() {
    const rows = await api("/api/conversations");
    $("#conv-list").innerHTML = rows.length ? `
      <div class="panel"><table><tbody>
        ${rows.map((c) => `
          <tr class="clickable" onclick="showThread('${esc(c.peer)}')">
            <td style="width:260px;font-weight:500">${esc(c.peer)}</td>
            <td class="num" style="width:80px">${c.msg_count} 封</td>
            <td>${esc(trunc(c.last_subject, 50))}</td>
            <td style="width:160px;color:var(--postmark)" class="num">${fmtTime(c.last_time)}</td>
          </tr>`).join("")}
      </tbody></table></div>` :
      '<div class="empty"><strong>暂无会话</strong>往来邮件会自动按联系人归档到这里</div>';
  },

  async compose() {
    if (!ACCOUNTS.length) ACCOUNTS = await api("/api/accounts");
    const tpls = await api("/api/templates");
    const active = ACCOUNTS.filter((a) => a.active);
    if (!active.length) {
      $("#v-compose").innerHTML = '<div class="empty"><strong>还没有绑定邮箱</strong>先到「邮箱管理」添加一个发件账号</div>';
      return;
    }
    $("#v-compose").innerHTML = `
      <div class="panel"><div class="panel-body">
        <div class="field"><label>发件账号</label>
          <select id="c-account">${active.map((a) => `<option value="${a.id}">${esc(a.name)} · ${esc(a.email)}</option>`).join("")}</select></div>
        <div class="field"><label>收件人</label><input type="email" id="c-to" placeholder="someone@example.com"></div>
        <div class="field"><label>抄送（可选）</label><input type="text" id="c-cc" placeholder="a@x.com, b@y.com"></div>
        <div class="field"><label>主题</label><input type="text" id="c-subject"></div>
        <div class="field"><label>正文</label><textarea id="c-body" placeholder="邮件正文…" rows="8"></textarea></div>
        <div class="field"><label>套用模板（可选，填充主题与正文）</label>
          <select id="c-template"><option value="">不使用模板</option>
            ${tpls.map((t) => `<option value="${t.id}">${esc(t.name)} · ${esc(t.category)}</option>`).join("")}</select></div>
        <button class="btn primary" onclick="doSend()">发送</button>
      </div></div>`;
    $("#c-template").onchange = async () => {
      const id = $("#c-template").value;
      if (!id) return;
      const t = tpls.find((x) => x.id == id);
      $("#c-subject").value = t.subject;
      $("#c-body").value = t.body;
    };
  },

  async campaigns() {
    if (!ACCOUNTS.length) ACCOUNTS = await api("/api/accounts");
    const rows = await api("/api/campaigns");
    $("#campaign-list").innerHTML = `
      <div class="list-toolbar">
        <button class="btn primary" onclick="newCampaign()">新建群发任务</button>
      </div>
      ${rows.length ? campTable(rows, true) :
        '<div class="empty"><strong>还没有群发任务</strong>创建任务后导入客户名单，系统按限速自动发送并追踪回复</div>'}`;
  },

  async tracking() {
    const camps = await api("/api/campaigns");
    if (!camps.length) { $("#tracking-body").innerHTML = '<div class="empty"><strong>暂无数据</strong>群发任务发送后这里会显示回复、退信、未回复的合并统计</div>'; return; }
    const merged = await api("/api/track/merge");
    const tab = window._trackTab || "unreplied";
    const list = await api(`/api/track/${tab}`);
    $("#tracking-body").innerHTML = `
      <div class="panel"><div class="panel-body">
        <div class="metrics" style="margin:0;grid-template-columns:repeat(5,1fr)">
          <div><div class="metric-label">总收件人</div><div class="metric-value num">${merged.total}</div></div>
          <div><div class="metric-label">已发送</div><div class="metric-value num">${merged.sent}</div></div>
          <div><div class="metric-label">已回复</div><div class="metric-value num" style="color:var(--green)">${merged.replied}</div></div>
          <div><div class="metric-label">退信</div><div class="metric-value num" style="color:var(--stamp)">${merged.bounced}</div></div>
          <div><div class="metric-label">回复率</div><div class="metric-value num">${merged.reply_rate}%</div></div>
        </div>
      </div></div>
      <div class="panel">
        <div class="panel-head"><h3>反馈明细（全部任务合并）</h3>
          <div class="toolbar-actions">
            ${["unreplied|未回复", "replied|已回复", "bounced|退信"].map((t) => {
              const [k, label] = t.split("|");
              return `<button class="btn small ${tab === k ? "primary" : ""}" onclick="setTrackTab('${k}')">${label}</button>`;
            }).join("")}
          </div></div>
        <div class="panel-body">${feedbackTable(tab, list)}</div>
      </div>`;
  },

  async leads() {
    const level = window._leadLevel || "";
    const rows = await api("/api/leads" + (level ? "?level=" + level : ""));
    const counts = { high: 0, medium: 0, low: 0 };
    (await api("/api/leads")).forEach((l) => { if (counts[l.intent_level] != null) counts[l.intent_level]++; });
    $("#leads-body").innerHTML = `
      <div class="list-toolbar">
        ${[["", "全部"], ["high", "高意向"], ["medium", "中意向"], ["low", "低意向"]].map(([k, label]) =>
          `<button class="btn small ${level === k ? "primary" : ""}" onclick="setLeadLevel('${k}')">${label}${counts[k] != null ? ` (${counts[k]})` : ""}</button>`).join("")}
        <span style="margin-left:auto"></span>
        <button class="btn small" onclick="location.href='/api/leads/export${level ? "?level=" + level : ""}'">导出 CSV</button>
      </div>
      <div class="panel"><table><thead><tr>
        <th>客户</th><th>公司</th><th>意向</th><th class="num">分数</th><th class="num">回复数</th><th>最近回复</th><th>操作</th>
      </tr></thead><tbody>
        ${rows.length ? rows.map((l) => `
          <tr>
            <td><div style="font-weight:500">${esc(l.name || l.email)}</div><div style="font-size:12px;color:var(--postmark-light)">${esc(l.email)}</div></td>
            <td>${esc(l.company || "—")}</td>
            <td><span class="chip ${l.intent_level}">${l.intent_label}</span></td>
            <td class="num">${l.intent_score}</td>
            <td class="num">${l.reply_count}</td>
            <td style="color:var(--postmark)" class="num">${fmtTime(l.latest_reply_at)}</td>
            <td><button class="btn small danger" onclick="rejectLead('${esc(l.email)}')">拒绝·拉黑</button></td>
          </tr>`).join("") :
          '<tr><td colspan="7"><div class="empty"><strong>暂无意向客户</strong>系统会根据群发回复内容自动评分并标记高意向客户</div></td></tr>'}
      </tbody></table></div>`;
  },

  async templates() {
    const rows = await api("/api/templates");
    $("#template-list").innerHTML = `
      <div class="list-toolbar"><button class="btn primary" onclick="newTemplate()">新建模板</button></div>
      ${rows.length ? `<div class="panel"><table><thead><tr><th>名称</th><th>分类</th><th>主题</th><th></th></tr></thead><tbody>
        ${rows.map((t) => `<tr>
          <td style="font-weight:500">${esc(t.name)}</td><td>${esc(t.category)}</td>
          <td>${esc(trunc(t.subject, 40))}</td>
          <td style="text-align:right"><button class="btn small danger" onclick="delTemplate(${t.id})">删除</button></td>
        </tr>`).join("")}</tbody></table></div>` :
      '<div class="empty"><strong>模板库是空的</strong>创建带 {{变量}} 占位符的模板，群发和写信时直接套用</div>'}`;
  },

  async blacklist() {
    const rows = await api("/api/blacklist");
    $("#blacklist-body").innerHTML = `
      <div class="list-toolbar">
        <input type="email" id="bl-email" placeholder="邮箱地址" style="max-width:280px">
        <input type="text" id="bl-reason" placeholder="原因（可选）" style="max-width:280px">
        <button class="btn primary" onclick="addBlacklist()">加入黑名单</button>
      </div>
      ${rows.length ? `<div class="panel"><table><thead><tr><th>邮箱</th><th>原因</th><th>来源</th><th>时间</th><th></th></tr></thead><tbody>
        ${rows.map((b) => `<tr>
          <td class="num">${esc(b.email)}</td><td>${esc(b.reason || "—")}</td>
          <td>${esc({ manual: "手动", auto_bounce: "退信自动", rejected: "拒绝" }[b.source] || b.source)}</td>
          <td class="num" style="color:var(--postmark)">${fmtTime(b.created_at)}</td>
          <td style="text-align:right"><button class="btn small" onclick="delBlacklist('${esc(b.email)}')">移除</button></td>
        </tr>`).join("")}</tbody></table></div>` :
      '<div class="empty"><strong>黑名单为空</strong>黑名单邮箱会在普邮发送时拦截、群发导入时自动剔除</div>'}`;
  },

  async accounts() {
    ACCOUNTS = await api("/api/accounts");
    $("#account-list").innerHTML = `
      <div class="list-toolbar"><button class="btn primary" onclick="newAccount()">绑定新邮箱</button></div>
      ${ACCOUNTS.length ? ACCOUNTS.map((a) => `
        <div class="panel"><div class="panel-body">
          <div style="display:flex;justify-content:space-between;align-items:flex-start">
            <div>
              <div style="font-weight:600;font-size:15px">${esc(a.name)} ${a.active ? "" : '<span class="chip none">已停用</span>'}</div>
              <div class="num" style="color:var(--postmark)">${esc(a.email)}</div>
              <div style="font-size:12px;color:var(--postmark-light);margin-top:6px">
                显示名 ${esc(a.display_name || "（未设置）")} · 服务商 ${esc(a.provider)} ·
                SMTP ${esc(a.smtp_host)}:${a.smtp_port} · IMAP ${esc(a.imap_host)}:${a.imap_port}</div>
            </div>
            <div class="toolbar-actions">
              <button class="btn small" onclick="testAccount(${a.id})">测试连接</button>
              <button class="btn small" onclick="showProfiles(${a.id})">署名身份</button>
              ${a.active ? `<button class="btn small danger" onclick="delAccount(${a.id})">停用</button>` : ""}
            </div>
          </div>
          <div class="quota-bar" style="margin-top:14px"><div class="quota-fill" style="width:${a.daily_limit ? Math.round(a.today_sent / a.daily_limit * 100) : 0}%"></div></div>
          <div style="font-size:12px;color:var(--postmark);margin-top:4px" class="num">今日已发 ${a.today_sent} / ${a.daily_limit} · 群发间隔 ${a.send_interval}s</div>
        </div></div>`).join("") :
      '<div class="empty"><strong>还没有绑定邮箱</strong>支持 Gmail、QQ、163、企业邮箱等自有邮箱</div>'}`;
  },
};

// ------------------------------------------------------------------ 公共渲染
function campTable(rows, clickable) {
  const statusCn = { draft: "草稿", running: "发送中", paused: "已暂停", completed: "已完成" };
  return `<table><thead><tr>
      <th>任务</th><th>发件账号</th><th>状态</th><th class="num">进度</th>
      <th class="num">已回复</th><th class="num">退信</th>${clickable ? "<th></th>" : ""}
    </tr></thead><tbody>
    ${rows.map((c) => {
      const done = c.sent_cnt + c.replied_cnt + c.bounced_cnt;
      const sent = c.sent_cnt + c.replied_cnt + c.bounced_cnt;
      return `<tr class="${clickable ? "clickable" : ""}" ${clickable ? `onclick="campaignDetail(${c.id})"` : ""}>
        <td style="font-weight:500">${esc(c.name)}</td>
        <td style="color:var(--postmark)" class="num">${esc(c.account_email)}</td>
        <td><span class="status s-${c.status === "running" ? "pending" : "sent"}" style="color:${c.status === "completed" ? "var(--green)" : c.status === "running" ? "var(--blue)" : "var(--postmark)"}">${statusCn[c.status]}</span></td>
        <td class="num">${done}/${c.total}</td>
        <td class="num" style="color:var(--green)">${c.replied_cnt}</td>
        <td class="num" style="color:var(--stamp)">${c.bounced_cnt}</td>
        ${clickable ? `<td style="text-align:right"><button class="btn small" onclick="event.stopPropagation();campaignDetail(${c.id})">明细</button></td>` : ""}
      </tr>`;
    }).join("")}
  </tbody></table>`;
}

function feedbackTable(tab, list) {
  if (!list.length) return '<div class="empty">暂无记录</div>';
  if (tab === "replied") return `<table><thead><tr><th>邮箱</th><th>姓名</th><th>任务</th><th>回复摘要</th><th>时间</th></tr></thead><tbody>
    ${list.map((r) => `<tr><td class="num">${esc(r.email)}</td><td>${esc(r.display_name || "—")}</td>
      <td>${esc(r.campaign_name)}</td><td>${esc(trunc(r.reply_excerpt, 44))}</td>
      <td class="num" style="color:var(--postmark)">${fmtTime(r.replied_at)}</td></tr>`).join("")}</tbody></table>`;
  if (tab === "bounced") return `<table><thead><tr><th>邮箱</th><th>任务</th><th>退信原因</th><th>时间</th></tr></thead><tbody>
    ${list.map((r) => `<tr><td class="num">${esc(r.email)}</td><td>${esc(r.campaign_name)}</td>
      <td style="color:var(--stamp)">${esc(trunc(r.bounce_reason, 50))}</td>
      <td class="num" style="color:var(--postmark)">${fmtTime(r.sent_at)}</td></tr>`).join("")}</tbody></table>`;
  return `<table><thead><tr><th>邮箱</th><th>姓名</th><th>公司</th><th>任务</th><th>发送时间</th></tr></thead><tbody>
    ${list.map((r) => `<tr><td class="num">${esc(r.email)}</td><td>${esc(r.display_name || "—")}</td>
      <td>${esc(r.company || "—")}</td><td>${esc(r.campaign_name)}</td>
      <td class="num" style="color:var(--postmark)">${fmtTime(r.sent_at)}</td></tr>`).join("")}</tbody></table>`;
}

// ------------------------------------------------------------------ 邮件阅读/线程
async function readMessage(id) {
  const m = await api("/api/messages/" + id);
  let bodyHtml = "";
  if (m.has_html) {
    const r = await api(`/api/messages/${id}/html`);
    if (r.html) bodyHtml = mailFrame(id, r.html, r.remote_count);
  }
  openModal(m.subject || "(无主题)", `
    <div class="meta">
      发件人 ${esc(m.from_addr)}<br>
      时间 ${fmtTime(m.sent_at || m.received_at)}<br>
      经 ${esc(m.account_email || "")}
    </div>
    ${bodyHtml || `<div class="content">${esc(m.body_text)}</div>`}
    ${m.direction === "in" ? `<div style="margin-top:18px"><button class="btn primary" onclick="replyTo('${esc(m.from_addr)}')">回复此人</button></div>` : ""}`);
  if (!m.is_read) { await api(`/api/messages/${id}/read`, { method: "POST" }); refreshInboxBadge(); }
}

// 沙箱 iframe 渲染邮件 HTML；remote_count>0 时提供"显示图片"按钮（按需加载远程图，防追踪像素）
function mailFrame(mid, html, remoteCount) {
  const fid = "mf_" + Math.random().toString(36).slice(2);
  setTimeout(() => {
    const f = document.getElementById(fid);
    if (!f) return;
    const apply = (h) => {
      f.srcdoc = "<!DOCTYPE html><meta charset='utf-8'>"
        + "<meta http-equiv='Content-Security-Policy' content=\"default-src 'none'; img-src 'self' http: https: data:; style-src 'unsafe-inline'; base-uri 'none'\">"
        + h;
    };
    apply(html);
    const fit = () => { try { f.style.height = Math.min(f.contentDocument.documentElement.scrollHeight + 20, 5000) + "px"; } catch {} };
    f.onload = () => { fit(); setTimeout(fit, 300); setTimeout(fit, 1200); };
    window["showImgs_" + fid] = async (btn) => {
      btn.disabled = true; btn.textContent = "加载图片中…";
      try {
        const r = await api(`/api/messages/${mid}/html?remote=1`);
        apply(r.html); setTimeout(fit, 300); setTimeout(fit, 1200);
        btn.style.display = "none";
      } catch (e) { btn.disabled = false; btn.textContent = "重试"; }
    };
  }, 0);
  return `<iframe id="${fid}" sandbox="allow-same-origin" class="mail-frame" title="邮件正文"></iframe>`
    + (remoteCount ? `<div class="img-bar"><button class="btn small" onclick="window['showImgs_${fid}'](this)">显示图片（${remoteCount}）</button> <span>为避免追踪像素，远程图片默认不加载</span></div>` : "");
}


async function showThread(peer) {
  const rows = await api("/api/conversations/thread?peer=" + encodeURIComponent(peer));
  openModal("与 " + peer + " 的会话",
    (rows.length ? rows.map((m) => `
      <div style="padding:12px 0;border-bottom:1px solid var(--paper-2)">
        <div style="font-size:12px;color:var(--postmark);margin-bottom:4px">
          ${m.direction === "out" ? "→ 发出" : "← 收到"} · ${fmtTime(m.sent_at || m.received_at)}</div>
        <div style="white-space:pre-wrap">${esc(trunc(m.body_text, 400))}</div>
      </div>`).join("") : "暂无往来邮件") +
    `<div style="margin-top:16px"><button class="btn primary" onclick="replyTo('${esc(peer)}')">回复</button></div>`);
}

function replyTo(peer) {
  closeModal();
  showView("compose");
  setTimeout(() => {
    $("#c-to").value = peer;
    if ($("#c-subject") && !$("#c-subject").value) $("#c-subject").focus();
  }, 60);
}

// ------------------------------------------------------------------ 操作
async function doSend() {
  const payload = {
    account_id: +$("#c-account").value, to: $("#c-to").value.trim(),
    subject: $("#c-subject").value, body: $("#c-body").value, cc: $("#c-cc").value.trim() || null,
  };
  if (!payload.to) return toast("请填写收件人", true);
  try {
    await api("/api/send", { method: "POST", body: payload });
    toast("已发送 → " + payload.to);
    $("#c-to").value = ""; $("#c-subject").value = ""; $("#c-body").value = ""; $("#c-cc").value = "";
  } catch (e) { toast(e.message, true); }
}

async function newCampaign() {
  if (!ACCOUNTS.length) ACCOUNTS = await api("/api/accounts");
  const active = ACCOUNTS.filter((a) => a.active);
  const tpls = await api("/api/templates");
  openModal("新建群发任务", `
    <div class="field"><label>任务名称</label><input type="text" id="k-name" placeholder="如：九月渠道触达"></div>
    <div class="row">
      <div class="field"><label>发件账号</label><select id="k-account">${active.map((a) => `<option value="${a.id}">${esc(a.name)}</option>`).join("")}</select></div>
      <div class="field"><label>套用模板</label><select id="k-template"><option value="">不用</option>${tpls.map((t) => `<option value="${t.id}">${esc(t.name)}</option>`).join("")}</select></div>
    </div>
    <div class="field"><label>主题（支持 {{name}} 变量）</label><input type="text" id="k-subject"></div>
    <div class="field"><label>正文（支持 {{name}}、{{company}} 等）</label><textarea id="k-body" rows="6"></textarea></div>
    <div class="field"><label>群发间隔（秒，留空用账号默认）</label><input type="number" id="k-interval" placeholder="30"></div>
    <div class="field"><label>收件人（CSV，首行含 email 列，可选 name/company 列，逗号分隔）</label>
      <textarea id="k-recipients" rows="5" placeholder="email,name,company&#10;alice@a.com,爱丽丝,甲公司&#10;bob@b.com,鲍勃,乙公司"></textarea></div>
    <div style="display:flex;gap:10px"><button class="btn primary" onclick="createCampaign()">创建并开始</button>
      <button class="btn" onclick="closeModal()">取消</button></div>`);
  $("#k-template").onchange = () => {
    const id = $("#k-template").value;
    const t = tpls.find((x) => x.id == id);
    if (t) { $("#k-subject").value = t.subject; $("#k-body").value = t.body; }
  };
}

async function createCampaign() {
  const csv = $("#k-recipients").value.trim();
  let recipients = null;
  if (csv) {
    const lines = csv.split(/\r?\n/).filter(Boolean);
    const head = lines[0].split(",").map((h) => h.trim().toLowerCase());
    recipients = lines.slice(1).map((line) => {
      const cells = line.split(",");
      const o = {};
      head.forEach((h, i) => (o[h] = (cells[i] || "").trim()));
      return o;
    });
  }
  try {
    const r = await api("/api/campaigns", {
      method: "POST",
      body: {
        name: $("#k-name").value, account_id: +$("#k-account").value,
        subject: $("#k-subject").value, body: $("#k-body").value,
        template_id: +$("#k-template").value || null,
        send_interval: $("#k-interval").value ? +$("#k-interval").value : null,
        recipients,
      },
    });
    if (recipients) await api(`/api/campaigns/${r.id}/start`, { method: "POST" });
    closeModal();
    toast(`任务已创建${recipients ? `，导入 ${r.imported} 人并开始发送` : "（草稿）"}`);
    VIEWS.campaigns();
  } catch (e) { toast(e.message, true); }
}

async function campaignDetail(cid) {
  const [stats, recips] = await Promise.all([
    api(`/api/campaigns/${cid}/stats`), api(`/api/campaigns/${cid}/recipients`)]);
  const total = stats.total || 1;
  const seg = (n, cls) => n ? `<span class="${cls}" style="width:${n / total * 100}%"></span>` : "";
  openModal(`任务：${stats.name}`, `
    <div style="font-size:13px;color:var(--postmark);margin-bottom:8px">状态：${stats.status} · 回复率 ${stats.reply_rate}%</div>
    <div class="progress">
      ${seg(stats.replied, "p-replied")}${seg(stats.sent - stats.replied - stats.bounced, "p-sent")}${seg(stats.bounced, "p-bounced")}${seg(stats.pending, "p-pending")}</div>
    <div style="display:flex;gap:16px;font-size:12.5px;margin-bottom:16px">
      <span><span class="status p-replied" style="color:var(--green)"></span>已回复 ${stats.replied}</span>
      <span><span style="display:inline-block;width:8px;height:8px;background:var(--postmark-light);border-radius:50%"></span>已发送 ${stats.sent - stats.replied - stats.bounced}</span>
      <span><span style="display:inline-block;width:8px;height:8px;background:var(--stamp);border-radius:50%"></span>退信 ${stats.bounced}</span>
      <span><span style="display:inline-block;width:8px;height:8px;background:#D8D6CF;border-radius:50%"></span>待发 ${stats.pending}</span>
    </div>
    <table><thead><tr><th>收件人</th><th>姓名</th><th>状态</th></tr></thead><tbody>
      ${recips.slice(0, 200).map((r) => `<tr><td class="num">${esc(r.email)}</td><td>${esc(r.display_name || "—")}</td>
        <td><span class="status s-${r.status}">${statusCn(r.status)}</span></td></tr>`).join("")}
    </tbody></table>
    ${stats.status === "draft" || stats.status === "paused" ? `<div style="margin-top:16px"><button class="btn primary" onclick="startCampaign(${cid})">开始/继续发送</button></div>` : ""}
    ${stats.status === "running" ? `<div style="margin-top:16px"><button class="btn" onclick="pauseCampaign(${cid})">暂停</button></div>` : ""}`);
}
function statusCn(s) {
  return { pending: "待发送", queued: "队列中", sent: "已发送", bounced: "退信", replied: "已回复", skipped_blacklist: "黑名单跳过" }[s] || s;
}
async function startCampaign(cid) { try { await api(`/api/campaigns/${cid}/start`, { method: "POST" }); closeModal(); toast("已加入发送队列"); VIEWS.campaigns(); } catch (e) { toast(e.message, true); } }
async function pauseCampaign(cid) { await api(`/api/campaigns/${cid}/pause`, { method: "POST" }); closeModal(); toast("已暂停"); VIEWS.campaigns(); }
function setTrackTab(k) { window._trackTab = k; VIEWS.tracking(); }
function setLeadLevel(k) { window._leadLevel = k; VIEWS.leads(); }
async function rejectLead(email) {
  if (!confirm(`确认将 ${email} 标记为已拒绝并加入黑名单？后续群发将自动排除。`)) return;
  await api(`/api/leads/${encodeURIComponent(email)}/blacklist`, { method: "POST" });
  toast("已拒绝并加入黑名单"); VIEWS.leads();
}

async function newTemplate() {
  openModal("新建模板", `
    <div class="field"><label>模板名称</label><input type="text" id="t-name"></div>
    <div class="field"><label>分类</label><select id="t-cat"><option>首次触达</option><option>跟进</option><option>提案</option><option>感谢</option><option>通用</option></select></div>
    <div class="field"><label>主题（支持 {{name}} {{company}}）</label><input type="text" id="t-subject"></div>
    <div class="field"><label>正文</label><textarea id="t-body" rows="8"></textarea></div>
    <div style="display:flex;gap:10px"><button class="btn primary" onclick="saveTemplate()">保存</button>
      <button class="btn" onclick="closeModal()">取消</button></div>`);
}
async function saveTemplate() {
  try {
    await api("/api/templates", { method: "POST", body: {
      name: $("#t-name").value, category: $("#t-cat").value,
      subject: $("#t-subject").value, body: $("#t-body").value } });
    closeModal(); toast("模板已保存"); VIEWS.templates();
  } catch (e) { toast(e.message, true); }
}
async function delTemplate(id) { if (confirm("删除该模板？")) { await api("/api/templates/" + id, { method: "DELETE" }); VIEWS.templates(); } }

async function addBlacklist() {
  const email = $("#bl-email").value.trim();
  if (!email) return toast("请输入邮箱", true);
  try { await api("/api/blacklist", { method: "POST", body: { email, reason: $("#bl-reason").value } }); toast("已加入黑名单"); VIEWS.blacklist(); }
  catch (e) { toast(e.message, true); }
}
async function delBlacklist(email) { await api("/api/blacklist/" + encodeURIComponent(email), { method: "DELETE" }); VIEWS.blacklist(); }

async function newAccount() {
  const provs = await api("/api/providers");
  const provOpts = Object.entries(provs).map(([k, v]) => `<option value="${k}">${k} — ${v.smtp_host}</option>`).join("");
  openModal("绑定邮箱", `
    <div class="field"><label>账号别名</label><input type="text" id="a-name" placeholder="如：公司主邮箱"></div>
    <div class="row">
      <div class="field"><label>服务商</label><select id="a-provider" onchange="toggleProvider()"><option value="custom">自定义 / 企业邮箱</option>${provOpts}</select></div>
      <div class="field"><label>每日发送上限</label><input type="number" id="a-limit" value="300"></div>
    </div>
    <div class="field"><label>邮箱地址（登录用户名）</label><input type="email" id="a-email"></div>
    <div class="field"><label>密码 / 授权码${"" }<span id="a-provnote" style="color:var(--postmark-light);font-weight:normal"></span></label>
      <input type="password" id="a-password"></div>
    <div class="field"><label>发件人显示名（收件人看到的名字）</label><input type="text" id="a-display"></div>
    <div id="a-custom" class="hidden">
      <div class="row"><div class="field"><label>SMTP 服务器</label><input type="text" id="a-smtphost"></div>
        <div class="field"><label>SMTP 端口</label><input type="number" id="a-smtpport" value="465"></div></div>
      <div class="row"><div class="field"><label>IMAP 服务器</label><input type="text" id="a-imaphost"></div>
        <div class="field"><label>IMAP 端口</label><input type="number" id="a-imapport" value="993"></div></div>
    </div>
    <div style="display:flex;gap:10px"><button class="btn primary" onclick="saveAccount()">保存并测试连接</button>
      <button class="btn" onclick="closeModal()">取消</button></div>`);
  window._provs = provs;
}
function toggleProvider() {
  const p = $("#a-provider").value;
  const custom = $("#a-custom");
  const note = $("#a-provnote");
  if (p === "custom") { custom.classList.remove("hidden"); note.textContent = ""; }
  else { custom.classList.add("hidden"); note.textContent = " · " + (window._provs[p] ? window._provs[p].note : ""); }
}
async function saveAccount() {
  const body = {
    name: $("#a-name").value, email: $("#a-email").value.trim(), username: $("#a-email").value.trim(),
    password: $("#a-password").value, provider: $("#a-provider").value,
    display_name: $("#a-display").value, daily_limit: +$("#a-limit").value,
  };
  if ($("#a-provider").value === "custom") {
    Object.assign(body, { smtp_host: $("#a-smtphost").value, smtp_port: +$("#a-smtpport").value,
      imap_host: $("#a-imaphost").value, imap_port: +$("#a-imapport").value });
  }
  try {
    const r = await api("/api/accounts", { method: "POST", body });
    closeModal();
    if (r.test.smtp && r.test.imap) toast("绑定成功，连接测试通过");
    else toast("账号已保存，但连接测试失败：" + (r.test.errors || []).join("; "), true);
    VIEWS.accounts();
  } catch (e) { toast(e.message, true); }
}
async function testAccount(id) {
  const r = await api(`/api/accounts/${id}/test`, { method: "POST" });
  toast(r.smtp && r.imap ? "连接正常" : "失败：" + r.errors.join("; "), !(r.smtp && r.imap));
}
async function delAccount(id) { if (confirm("停用该账号？")) { await api("/api/accounts/" + id, { method: "DELETE" }); VIEWS.accounts(); } }
async function showProfiles(aid) {
  const profs = await api(`/api/accounts/${aid}/profiles`);
  openModal("发件人身份（多套署名）", `
    ${profs.length ? `<table><tbody>${profs.map((p) => `<tr><td>${esc(p.name)}</td><td>${esc(p.display_name)}</td>
      <td>${p.is_default ? '<span class="chip none">默认</span>' : ""}</td></tr>`).join("")}</tbody></table>` : '<div class="empty">尚未添加备用身份，默认使用账号显示名</div>'}
    <div style="margin-top:16px;border-top:1px solid var(--paper-2);padding-top:16px">
      <div class="row"><div class="field"><label>场景名</label><input type="text" id="p-name" placeholder="如：商务合作"></div>
      <div class="field"><label>显示名</label><input type="text" id="p-display"></div></div>
      <div class="field"><label>签名</label><textarea id="p-sig" rows="3" placeholder="自动附加在正文末尾"></textarea></div>
      <button class="btn primary" onclick="addProfile(${aid})">添加身份</button></div>`);
}
async function addProfile(aid) {
  try {
    await api(`/api/accounts/${aid}/profiles`, { method: "POST", body: {
      name: $("#p-name").value, display_name: $("#p-display").value, signature: $("#p-sig").value, is_default: true } });
    closeModal(); toast("已添加身份"); VIEWS.accounts();
  } catch (e) { toast(e.message, true); }
}

// ------------------------------------------------------------------ 刷新
function fillAccountSelect(sel) {
  const active = ACCOUNTS.filter((a) => a.active);
  const cur = $(sel).value;
  $(sel).innerHTML = '<option value="">全部邮箱</option>' + active.map((a) => `<option value="${a.id}">${esc(a.name)}</option>`).join("");
  $(sel).value = cur;
}
async function refreshInboxBadge() {
  const d = await api("/api/dashboard");
  $("#inbox-badge").textContent = d.unread || "";
  const accs = await api("/api/accounts");
  const totalSent = accs.filter((a) => a.active).reduce((s, a) => s + a.today_sent, 0);
  const cap = accs.filter((a) => a.active).reduce((s, a) => s + a.daily_limit, 0);
  $("#quota").textContent = `今日发送 ${totalSent} / ${cap}`;
}

// ------------------------------------------------------------------ 初始化
document.addEventListener("DOMContentLoaded", async () => {
  $$(".nav-item").forEach((b) => (b.onclick = () => showView(b.dataset.view)));
  $("#modal-close").onclick = closeModal;
  $("#modal").onclick = (e) => { if (e.target.id === "modal") closeModal(); };
  $("#inbox-unread").onchange = VIEWS.inbox;
  $("#inbox-account").onchange = VIEWS.inbox;
  $("#poll-btn").onclick = async () => {
    const btn = $("#poll-btn"); btn.disabled = true; btn.textContent = "拉取中…";
    try {
      const r = await api("/api/poll", { method: "POST" });
      toast(`拉取完成：新 ${r.new} · 回复 ${r.replies} · 退信 ${r.bounces}`);
      await refreshInboxBadge();
      if (!$("#v-inbox").classList.contains("hidden")) VIEWS.inbox();
    } catch (e) { toast(e.message, true); }
    btn.disabled = false; btn.textContent = "刷新收件箱";
  };
  ACCOUNTS = await api("/api/accounts").catch(() => []);
  await refreshInboxBadge();
  showView("dashboard");
  setInterval(refreshInboxBadge, 30000);
});
