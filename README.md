# 邮件中心系统（Mail Center）

基于自有邮箱的点对点邮件管理 + 群发追踪一体化系统。提供 **Web 界面** 和 **命令行** 两种操作方式。

## 启动方式

### 方式一：双击启动（推荐）

双击项目根目录下的 **`启动邮件中心.bat`**，服务启动后浏览器自动打开 <http://127.0.0.1:8000>。
保持黑色窗口开启即是系统在运行（后台调度器在其中工作），关闭窗口即停止。

### 方式二：命令行启动

```bash
cd C:\Users\TONYDOGZ\Desktop\Gmail
python mailcenter.py web
```

Web 界面包含：统一收件箱、会话、写信（可套模板）、群发任务、追踪统计、意向客户、模板库、黑名单、邮箱管理。后台收件轮询与群发发送队列随服务常驻运行。

命令行功能（`python mailcenter.py <命令组> ...`）仍全部可用，适合脚本化操作。

## 功能概览

| 模块 | 说明 |
|------|------|
| 邮箱管理 | 绑定自有邮箱（Gmail / QQ / 163 / 企业邮箱等），自动填充服务器配置 |
| 普邮管理 | 点对点发送、会话线程、统一收件箱、回复 |
| 群发任务 | 创建任务、导入 CSV 收件人、限速发送、每日配额 |
| 追踪统计 | 多任务合并统计、回复/退信/未回复清单 |
| 意向客户 | 自动评分分级、一键筛选、导出 CSV |
| 模板库 | 变量占位符、分类管理、预览 |
| 黑名单 | 发送拦截、群发自动过滤、退信自动拉黑 |
| 发件人身份 | 同一邮箱多套署名（显示名 + 签名） |

## 安装

```bash
pip install -r requirements.txt
```

## 快速开始

### 1. 绑定邮箱

```bash
python mailcenter.py account add --provider gmail
# 支持的服务商：gmail / outlook / qq / 163 / aliyun / tencent_exmail / custom
```

按提示输入邮箱地址、授权码（Gmail 需应用专用密码，QQ/163 需授权码）。
系统会自动测试 SMTP + IMAP 连接。

### 2. 点对点发送邮件

```bash
python mailcenter.py mail send --to someone@example.com --subject "你好" --body "正文内容"
```

查看收件箱 / 会话：

```bash
python mailcenter.py mail inbox --unread
python mailcenter.py mail conversations
python mailcenter.py mail thread someone@example.com
python mailcenter.py mail reply someone@example.com --body "回复内容"
```

### 3. 群发任务

```bash
# 创建任务
python mailcenter.py campaign create "九月推广" --subject "{{name}}，关于合作" --body "{{name}} 你好..."

# 导入收件人（CSV 需含 email 列，可选 name / company）
python mailcenter.py campaign import 1 recipients.csv

# 开始发送（由后台调度器执行）
python mailcenter.py campaign start 1
```

### 4. 启动后台调度器

```bash
python mailcenter.py run
```

调度器做两件事：
- 定时轮询所有邮箱收件箱（处理回复、退信、意向提取）
- 驱动群发队列（按限速间隔逐封发送，检查每日配额）

### 5. 查看追踪数据

```bash
python mailcenter.py campaign stats 1          # 单任务统计
python mailcenter.py track merge 1 2 3         # 多任务合并
python mailcenter.py track unreplied --campaign-id 1
python mailcenter.py track bounced
python mailcenter.py leads list --level high   # 高意向客户
python mailcenter.py leads export --level high # 导出 CSV
```

## 收件人 CSV 格式

```csv
email,name,company,title
alice@example.com,爱丽丝,甲公司,采购经理
bob@example.com,鲍勃,乙公司,技术负责人
```

`email` 列必填，其余列会作为模板变量（`{{name}}`、`{{company}}`、`{{title}}`）。
黑名单中的邮箱会在导入时自动过滤。

## 限速与配额

- 每个邮箱可配置每日发送上限（默认 300）和群发间隔（默认 30 秒）
- 修改：`python mailcenter.py account update <编号> --daily-limit 200 --send-interval 60`
- 群发时系统会检查当日已发送量，超限自动暂停到次日

## 意向评分规则

系统根据回复内容自动判定意向等级：

| 信号 | 分值 |
|------|------|
| 高意向关键词（感兴趣/约时间/报价等） | +30/个 |
| 中意向关键词（了解一下/先看看等） | +10/个 |
| 低意向关键词（不需要/取消订阅等） | -40/个 |
| 回复中含提问 | +5 |

等级：high / medium / low / none，意向只升不降。

## 数据存储

所有数据在 `data/` 目录：

```
data/
├── mailcenter.db    SQLite 数据库
├── .secret_key      加密密钥（勿泄露、勿提交版本库）
└── exports/         导出的 CSV 文件
```

## 项目结构

```
mail_center/
├── config.py            全局配置（路径、预置服务商、评分关键词）
├── crypto.py            密码加密（Fernet）
├── db.py                数据库建表与连接
├── cli.py               命令行入口
├── core/
│   ├── parser.py        邮件解析（退信识别/线程头/变量渲染）
│   ├── smtp.py          SMTP 发送
│   └── imap.py          IMAP 收取（会话式，先入库后标记）
├── services/
│   ├── accounts.py      邮箱管理 + 发件人身份
│   ├── direct_mail.py   普邮（发送/会话/收件箱）
│   ├── campaigns.py     群发任务（限速/配额/发送队列）
│   ├── inbox.py         收件箱处理引擎（回复匹配/退信/意向）
│   ├── intent.py        意向评分规则
│   ├── templates.py     模板库
│   ├── blacklist.py     黑名单
│   └── tracking.py      统计与导出
├── engine/
│   └── scheduler.py     后台调度（轮询 + 发送队列）
├── web/
│   ├── app.py           Flask API（39 个端点）
│   └── __init__.py      Web 启动入口（API + 调度器）
└── static/
    ├── index.html       单页前端
    ├── styles.css       运营台账风格设计
    └── app.js           前端逻辑
```

## 注意事项

- Gmail 需要使用**应用专用密码**（非账号密码），在 Google 账号安全设置中生成
- QQ 邮箱 / 163 邮箱需要在设置中开启 IMAP 并获取**授权码**
- 群发间隔建议不低于 30 秒，日发量建议不超过邮箱服务商限制
- 退信累计达到 2 次的邮箱会自动加入黑名单
