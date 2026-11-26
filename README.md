# OpenGithubs 开源趋势中文深度汇总

自动抓取 [OpenGithubs](https://github.com/OpenGithubs) 的**日榜 / 周榜 / 月榜**，拉取各项目仓库信息与 `README.md`，通过大模型生成结构化的中文功能详解与适用场景汇总，并支持自动归档与群机器人推送。

## 📌 最新榜单直达

<!-- DIGEST_LINKS_START -->

- 📅 **日榜**：[📅 OpenGithubs 日榜中文深度汇总（2026-11-25）](reports/daily/LATEST.md)
- 📊 **周榜**：[📊 OpenGithubs 周榜中文深度汇总（2026-11-23）](reports/weekly/LATEST.md)
- 🏆 **月榜**：暂未生成

<!-- DIGEST_LINKS_END -->

---

## ✨ 特性

- **零外部依赖**：纯 Python 3.11 标准库实现，GitHub Actions 秒级启动。
- **结构化中文解读**：每个项目固定输出「一句话定位」「核心功能亮点」「适用场景」「领域标签」。
- **智能去重缓存**：热门项目连续上榜时自动复用 30 天内的缓存摘要并标记 `🔁`，大幅节省 API Token。
- **兼容主流大模型**：默认使用 **Google Gemini** 免费 API（`gemini-2.5-flash`），同时兼容 OpenAI / DeepSeek / 硅基流动等接口。
- **可选群消息推送**：支持飞书、钉钉、企业微信或通用 Webhook 机器人通知。

---

## 🚀 3 步部署到自己的 GitHub

### 1. 推送代码到 GitHub 仓库
将本项目推送到你的 GitHub 仓库（公开或私有仓库均可）。

### 2. 配置 Secrets 与 Variables
进入仓库的 **Settings → Secrets and variables → Actions**：

**Repository secrets（必填 / 选填）：**
| 名称 | 必填 | 说明 |
| :--- | :---: | :--- |
| `GEMINI_API_KEY` | ✅ | Google Gemini API Key |
| `SMTP_USER` | ❌ | 发件邮箱地址（如 `你的QQ号@qq.com`） |
| `SMTP_PASS` | ❌ | 邮箱 SMTP 授权码（QQ 邮箱在安全设置中生成） |
| `MAIL_TO` | ❌ | 收件邮箱（不填则默认发给 `SMTP_USER` 自己） |
| `WEBHOOK_URL` | ❌ | 飞书 / 钉钉 / 企业微信机器人 Webhook 地址（不填则只归档到仓库） |

**Repository variables（选填）：**
| 名称 | 默认值 | 说明 |
| :--- | :--- | :--- |
| `GEMINI_MODEL` | `gemini-2.5-flash` | 使用的 Gemini 模型名称 |
| `SMTP_HOST` | `smtp.qq.com` | SMTP 服务器地址 |
| `SMTP_PORT` | `465` | SMTP 端口（465 使用 SSL） |

### 3. 开启并测试运行
1. 打开仓库的 **Actions** 标签页，确保 Workflows 已启用。
2. 点击左侧 **OpenGithubs 中文趋势汇总** → **Run workflow**，选择 `daily` / `weekly` / `monthly` / `all` 即可立即体验。
3. 之后系统将在北京时间每天 09:00（日榜）、每周一 09:30（周榜）、每月 1 日 10:00（月榜）自动运行并提交报告。

---

## 🛠️ 本地调试

```bash
# 1. 仅测试抓取与解析（不调用大模型、不写缓存）
python scripts/digest.py --mode daily --limit 3 --dry-run

# 2. 实际调用 Gemini 生成（需先设置 GEMINI_API_KEY）
export GEMINI_API_KEY="你的Gemini_Key"
python scripts/digest.py --mode daily
```
