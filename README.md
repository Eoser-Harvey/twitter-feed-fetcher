# Twitter/X Tweet Fetcher

通过 GitHub Actions 海外节点抓取 X/Twitter 推文，解决国内网络无法访问 X.com 的问题。

## 工作原理

```
GitHub Actions (海外IP) → 抓取推文 → 提交到仓库 → 本地脚本通过 GitHub API 读取
```

## 抓取方法（按优先级）

1. **socialdata.tools API** — 最可靠，需要 API Key（免费额度）
2. **Twitter Guest API** — 无需凭证，使用公开 Bearer Token
3. **twikit** — 需要 Twitter 登录 Cookie（ct0 + auth_token）

## 配置

在仓库 Settings → Secrets and variables → Actions 中添加：

| Secret | 说明 | 必需 |
|--------|------|------|
| `SOCIALDATA_API_KEY` | socialdata.tools 的 API Key | 否（推荐） |
| `TWITTER_AUTH_TOKEN` | Twitter 登录 auth_token | 否 |
| `TWITTER_CT0` | Twitter 登录 ct0 cookie | 否 |

## 目标用户

| 用户名 | 显示名称 | User ID |
|--------|---------|---------|
| elonmusk | 马斯克 | 44196397 |
| cz_binance | CZ (赵长鹏) | 902926941413453824 |
| realDonaldTrump | 特朗普 | 25073877 |
| aleabitoreddit | Serenity (白毛股神) | 待解析 |

## 输出

- `tweets.json` — 抓取的推文数据，每次运行自动更新
- GitHub Actions artifacts — 7天保留

## 定时

每2小时自动运行一次（GitHub Actions cron，UTC时间）
