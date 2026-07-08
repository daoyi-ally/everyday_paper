# Daily GitHub Hot Projects Report

每天北京时间 09:00 自动整理并发送 GitHub 热点项目邮件：

- 技术创新项目：5 个
- 有趣项目：3 个
- 收件人默认：`3087130357@qq.com`

当前实现基于 GitHub Actions 定时运行，使用 Python 标准库脚本从 **GitHub Trending** 和 **GitHub Search API** 获取近期热点项目，生成中文 HTML/纯文本邮件并通过 SMTP 发送。

已接入 **Kimi（Moonshot）** 可选 AI 中文导读：如果配置 `MOONSHOT_API_KEY`，脚本会对最终入选的 8 个项目做 **1 次批量调用**，为每个项目生成更自然的“中文导读”和“推荐理由”；如果没有配置 Key，则自动回退到规则版中文文案。

## 已实现内容

- `.github/workflows/daily-github-report.yml`：每天北京时间 09:00 自动运行，也支持手动触发。
- `scripts/daily_github_report.py`：抓取、筛选、生成日报、发送邮件。
- `.env.example`：本地测试用环境变量模板。
- `.gitignore`：避免提交 `.env`、缓存和本地输出。

## 需要准备的物料

### 必需：SMTP 发件配置

在 GitHub 仓库的 `Settings -> Secrets and variables -> Actions -> New repository secret` 中添加：

| Secret 名称 | 说明 | QQ 邮箱示例 |
| --- | --- | --- |
| `SMTP_HOST` | SMTP 服务器 | `smtp.qq.com` |
| `SMTP_PORT` | SMTP 端口 | `465` |
| `SMTP_USERNAME` | 发件邮箱账号 | `yourname@qq.com` |
| `SMTP_PASSWORD` | SMTP 授权码/应用专用密码，不是登录密码 | QQ 邮箱授权码 |
| `MAIL_FROM` | 发件人邮箱 | `yourname@qq.com` |

收件人默认已经写入 workflow：`3087130357@qq.com`。如需修改，可改 `.github/workflows/daily-github-report.yml` 里的 `MAIL_TO`。

> QQ 邮箱通常需要在邮箱设置中开启 POP3/SMTP 服务，然后生成“授权码”。请不要把授权码提交到代码仓库。

### 建议：GitHub Token

可选但推荐添加：

| Secret 名称 | 说明 |
| --- | --- |
| `GH_TOKEN` | GitHub Personal Access Token，用于提高 API 限额 |

如果不填， GitHub Actions 会使用自动提供的 `github.token`。本地测试时如不想使用 API，可加 `--skip-api`，只用 GitHub Trending 页面数据。

### 推荐：Kimi API

如果你希望每个项目都有更自然、更适合中文邮件阅读的“中文导读”和“推荐理由”，推荐在 GitHub Secrets 中再添加：

| Secret 名称 | 说明 |
| --- | --- |
| `MOONSHOT_API_KEY` | Kimi / Moonshot Open Platform API Key |

可选环境变量：

| 环境变量 | 默认值 | 说明 |
| --- | --- | --- |
| `KIMI_MODEL` | `kimi-k2.6` | Kimi 模型名称 |
| `KIMI_MAX_TOKENS` | `4096` | 单次批量生成摘要的最大输出 token |

> 本项目会对当日最终入选的 8 个仓库发起 **1 次 Kimi 批量调用**，不是每个项目单独调用一次，所以费用和耗时都比较可控。

## 本地测试

复制环境变量模板：

```powershell
Copy-Item .env.example .env
```

编辑 `.env` 后，仅生成报告、不发邮件：

> 如需测试 AI 中文导读，请在 `.env` 中填写 `MOONSHOT_API_KEY`。如果不填，脚本会使用规则版中文导读作为兜底。

```powershell
python scripts/daily_github_report.py --dry-run --save-html out/sample-report.html
```

只使用 Trending、不调用 GitHub Search API：

```powershell
python scripts/daily_github_report.py --dry-run --skip-api --save-html out/sample-report.html
```

真实发送邮件：

```powershell
python scripts/daily_github_report.py
```

## GitHub Actions 定时

`.github/workflows/daily-github-report.yml` 使用 UTC 时间：

```yaml
cron: '0 1 * * *'
```

这对应北京时间每天 09:00。

也可以在 Actions 页面手动运行 `Daily GitHub Hot Projects Report` 验证邮件发送。
