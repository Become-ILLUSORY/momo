# Momo - 每日单词巩固阅读

自动获取[墨墨背单词](https://maimemo.com)当日学习单词，通过 AI 生成考研英语二阅读理解练习，PDF 格式发送到邮箱。支持多用户并发。

## 功能

- 从墨墨开放 API 动态获取今日学习单词数量和列表
- 将单词均分为 4 组，每组生成一篇阅读理解
- 每篇文章自然嵌入目标单词，单词同时出现在题干和选项中
- 生成完整 PDF：阅读文章 → 题目 → 答案与解析 → 全文中文翻译
- 通过 SMTP 发送 PDF 附件到指定邮箱
- 支持多用户并发处理（多线程）
- 支持 3 档难度（基础 / 标准 / 强化）
- 日志自动脱敏，保护用户隐私

## 运行

```bash
pip install -r requirements.txt
python main.py
```

## 环境变量

### 核心配置

| 变量 | 说明 |
|------|------|
| `OPENAI_BASE_URL` | OpenAI 兼容接口地址 |
| `OPENAI_API_KEY` | API Key |
| `OPENAI_MODEL` | 模型名称 |

### 邮件配置

| 变量 | 说明 |
|------|------|
| `SMTP_HOST` | 邮件服务器地址 |
| `SMTP_PORT` | 邮件服务器端口（默认 465） |
| `SMTP_USER` | 发件人邮箱 |
| `SMTP_PASS` | 邮箱密码/授权码 |

### 用户配置

**多用户模式**（推荐）：设置 `USERS` 环境变量为 JSON 字符串：

```json
[
  {"maimemo_token": "用户A的墨墨token", "email": "a@example.com", "difficulty": 2},
  {"maimemo_token": "用户B的墨墨token", "email": "b@example.com", "difficulty": 1}
]
```

**单用户模式**：设置 `MAIMEMO_TOKEN` + `EMAIL_TO`，兼容旧版。

### 可选配置

| 变量 | 说明 |
|------|------|
| `MAX_WORKERS` | 并发线程数（默认 3） |
| `DIFFICULTY_LEVEL` | 单用户模式难度：1/2/3（默认 2） |

## GitHub Actions

- **定时触发**：每天北京时间 22:30
- **手动触发**：支持选择难度等级

在 repo Settings → Secrets 中配置 `USERS`（JSON 字符串）和其他环境变量。

## 难度说明

| 档位 | 文章长度 | 句法难度 | 题型侧重 |
|------|---------|---------|---------|
| 1 基础档 | 350-380词 | 简单句，从句≤1层 | 4细节+1主旨，侧重词义识别 |
| 2 标准档 | 380-420词 | 贴合英二真题 | 3细节+1推理+1态度，覆盖四大陷阱 |
| 3 强化档 | 400-450词 | 嵌套从句、插入语 | 2细节+2推理+1态度，迷惑性更强 |

## 依赖

- Python 3.10+
- requests
- weasyprint（需系统安装 pango、CJK 字体）
