import os
import re
import json
import time
import shutil
import tempfile
import requests
import smtplib
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.base import MIMEBase
from email import encoders
from email.header import Header
from weasyprint import HTML
import html as _html_mod

# ===================== 配置读取 =====================
OPENAI_BASE_URL = os.getenv("OPENAI_BASE_URL", "").rstrip("/")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "")
SMTP_HOST = os.getenv("SMTP_HOST", "")
SMTP_PORT = int(os.getenv("SMTP_PORT", "465"))
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASS = os.getenv("SMTP_PASS", "")
MAX_WORKERS = int(os.getenv("MAX_WORKERS", "3"))

BEIJING_TZ = timedelta(hours=8)
TODAY = (datetime.utcnow() + BEIJING_TZ).strftime("%Y-%m-%d")


def mask_email(email: str) -> str:
    """将邮箱地址脱敏，如 u***@example.com"""
    if not email or "@" not in email:
        return "***"
    user, domain = email.split("@", 1)
    if len(user) <= 1:
        masked_user = "*"
    else:
        masked_user = user[0] + "***"
    return f"{masked_user}@{domain}"

DIFFICULTY_CONFIG = {
    1: {"name": "基础档", "word_count": "350-380",
        "sentence_rule": "长难句占比低于15%，从句嵌套不超过1层",
        "vocab_rule": "使用考研大纲基础释义，无生僻义",
        "question_rule": "4道细节题+1道主旨题，干扰项简单，侧重词义识别",
        "temperature": 0.5},
    2: {"name": "标准档（英二真题难度）", "word_count": "380-420",
        "sentence_rule": "长难句占比15%-20%，从句嵌套不超过2层",
        "vocab_rule": "使用考研大纲核心释义，优先真题高频熟词僻义",
        "question_rule": "3道细节题+1道推理题+1道主旨/态度题，干扰项覆盖偷换概念、正反混淆、无中生有、范围失当",
        "temperature": 0.6},
    3: {"name": "强化档（英二上限难度）", "word_count": "400-450",
        "sentence_rule": "长难句占比20%-25%，含嵌套从句、插入语等复杂结构",
        "vocab_rule": "侧重熟词僻义与一词多义，部分使用语境引申义",
        "question_rule": "2道细节题+2道推理判断题+1道主旨/态度题，干扰项迷惑性更强",
        "temperature": 0.65},
}


def load_users() -> list[dict]:
    """从 USERS 环境变量加载用户列表，回退到单用户模式"""
    users_json = os.getenv("USERS", "")
    if users_json:
        return json.loads(users_json)
    # 兼容单用户模式
    token = os.getenv("MAIMEMO_TOKEN", "")
    email = os.getenv("EMAIL_TO", "")
    difficulty = int(os.getenv("DIFFICULTY_LEVEL", "2"))
    if token and email:
        return [{"maimemo_token": token, "email": email, "difficulty": difficulty}]
    return []


# ===================== 墨墨 API =====================
def get_today_progress(token: str) -> dict:
    """获取今日学习进度：{finished, total, study_time}"""
    url = "https://open.maimemo.com/open/api/v1/study/get_study_progress"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    resp = requests.post(url, headers=headers, timeout=15)
    resp.raise_for_status()
    data = resp.json()
    return data.get("data", {}).get("progress", {})


def get_today_words(token: str, limit: int = 1000) -> list[str]:
    url = "https://open.maimemo.com/open/api/v1/study/get_today_items"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    resp = requests.post(url, headers=headers, json={"limit": limit}, timeout=15)
    resp.raise_for_status()
    data = resp.json()
    return [item["voc_spelling"] for item in data.get("data", {}).get("today_items", [])]


def split_into_groups(words: list, n: int = 4) -> list[list[str]]:
    if not words:
        return []
    base, rem = len(words) // n, len(words) % n
    groups, start = [], 0
    for i in range(n):
        size = base + (1 if i < rem else 0)
        groups.append(words[start:start + size])
        start += size
    return groups


# ===================== LLM 调用 (JSON模式) =====================
def call_llm_json(prompt: str, temperature: float, max_tokens: int = 16384) -> dict:
    url = f"{OPENAI_BASE_URL}/chat/completions"
    headers = {"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"}
    payload = {
        "model": OPENAI_MODEL,
        "messages": [
            {"role": "system", "content": "你是严谨的考研英语二命题专家。必须严格按要求输出纯JSON格式，不要输出任何Markdown标记或多余说明。"},
            {"role": "user", "content": prompt},
        ],
        "temperature": temperature,
        "max_tokens": max_tokens,
        "response_format": {"type": "json_object"},
    }

    for attempt in range(1, 4):
        try:
            resp = requests.post(url, headers=headers, json=payload, timeout=300)
            resp.raise_for_status()
            data = resp.json()
            finish_reason = data["choices"][0].get("finish_reason")
            content = data["choices"][0]["message"]["content"]

            if finish_reason == "length":
                usage = data.get("usage", {})
                r = usage.get("completion_tokens_details", {}).get("reasoning_tokens", 0)
                print(f"⚠️ 截断（reasoning:{r}），重试（{attempt}/3）")
                time.sleep(attempt * 10)
                continue

            content = content.strip()
            if content.startswith("```json"):
                content = content[7:-3].strip()
            elif content.startswith("```"):
                content = content[3:-3].strip()
            return json.loads(content)

        except json.JSONDecodeError as e:
            print(f"⚠️ JSON解析失败: {e}，重试（{attempt}/3）")
            if attempt < 3:
                time.sleep(attempt * 5)
        except requests.exceptions.ReadTimeout:
            print(f"⏳ 超时，重试（{attempt}/3）")
            if attempt < 3:
                time.sleep(attempt * 5)
        except Exception as e:
            raise RuntimeError(f"LLM异常: {e}")

    raise RuntimeError("LLM调用失败")


# ===================== 文件缓存 =====================
def save_json(work_dir, idx, name, data):
    with open(os.path.join(work_dir, f"p{idx}_{name}.json"), "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def load_json(work_dir, idx, name):
    path = os.path.join(work_dir, f"p{idx}_{name}.json")
    if not os.path.exists(path):
        return None
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


# ===================== 核心生成步骤 (2步走) =====================
def gen_article_and_translation(word_list, text_idx, config):
    prompt = f"""请根据提供的单词列表，撰写一篇符合考研英语二难度的文章及其中文翻译。

【命题要求】
1. 体裁：商业经济、社会生活、科技科普、教育文化（任选其一）。
2. 词数：{config['word_count']}词。
3. 必须包含所有给定的目标单词，每个单词用 **加粗** 标记。允许必要的语法变形。
4. 结构要求：现象引入 → 原因/数据展开 → 案例支撑 → 趋势/结论。
5. 语言风格：{config['sentence_rule']}；{config['vocab_rule']}。

【目标单词】
{', '.join(word_list)}

【输出JSON格式】
{{
  "article": "英文文章正文，段落间用\\n\\n分隔，目标单词用**包裹",
  "translation": "中文翻译，逐段对应，段落间用\\n\\n分隔"
}}"""
    return call_llm_json(prompt, config["temperature"])


def gen_questions_and_analysis(article, word_list, config):
    prompt = f"""基于以下文章，生成5道考研英语二阅读理解选择题，并附带答案和解析。

【文章内容】
{article}

【命题要求】
1. 题型分配：{config['question_rule']}。
2. 选项设置：四选项长度均衡。干扰项需基于文章信息修改，必须包含常见考研陷阱。
3. 解析格式：必须包含【定位原文】、【选项分析】（逐个分析对错原因）、【陷阱类型】。
4. 尽量在题干或选项中自然融入部分目标单词。

【目标单词】
{', '.join(word_list)}

【输出JSON格式】
{{
  "questions": [
    {{
      "question": "题干内容",
      "options": {{"A": "选项A", "B": "选项B", "C": "选项C", "D": "选项D"}},
      "answer": "正确答案字母",
      "analysis": "解析内容"
    }}
  ]
}}"""
    return call_llm_json(prompt, config["temperature"])


def generate_passage(work_dir, word_list, text_idx, config, tag=""):
    print(f"{tag}    📄 文章...", end=" ", flush=True)
    art_trans = load_json(work_dir, text_idx, "art_trans")
    if not art_trans:
        art_trans = gen_article_and_translation(word_list, text_idx, config)
        save_json(work_dir, text_idx, "art_trans", art_trans)
        print("✅")
    else:
        print("⏭️ (缓存)")

    print(f"{tag}    ❓ 题目...", end=" ", flush=True)
    qa = load_json(work_dir, text_idx, "qa")
    if not qa:
        qa = gen_questions_and_analysis(art_trans["article"], word_list, config)
        save_json(work_dir, text_idx, "qa", qa)
        print("✅")
    else:
        print("⏭️ (缓存)")

    return {
        "article": art_trans.get("article", ""),
        "translation": art_trans.get("translation", ""),
        "questions": qa.get("questions", []),
    }


# ===================== HTML 结构化解析 =====================
def _esc(text):
    return _html_mod.escape(text or "", quote=False)


def _bold(text):
    return re.sub(r'\*\*(.+?)\*\*', r'<strong>\1</strong>', text)


def article_to_html(text):
    paras = [p.strip() for p in text.strip().split("\n\n") if p.strip()]
    return "\n".join(f"<p>{_bold(_esc(p))}</p>" for p in paras)


def translation_to_html(text):
    paras = [p.strip() for p in text.strip().split("\n\n") if p.strip()]
    return "\n".join(f'<p style="text-indent:2em;">{_bold(_esc(p))}</p>' for p in paras)


def questions_to_html(questions):
    parts = ['<ol class="q-list">']
    for q in questions:
        parts.append('<li class="q-item">')
        parts.append(f'<div class="q-stem">{_esc(q.get("question",""))}</div>')
        parts.append('<ul class="opt-list">')
        for letter in "ABCD":
            parts.append(f'<li><span class="opt-letter">{letter}.</span> {_esc(q.get("options",{}).get(letter,""))}</li>')
        parts.append("</ul></li>")
    parts.append("</ol>")
    return "\n".join(parts)


def analysis_to_html(questions):
    parts = ['<ol class="analysis-list">']
    for i, q in enumerate(questions, 1):
        ana = _bold(_esc(q.get("analysis", "")))
        ana = re.sub(r'【(定位原文|选项分析|陷阱类型)】', r'<b>【\1】</b>', ana)
        ana = ana.replace("\n", "<br>")
        parts.append(f'<li class="analysis-item"><div class="a-content"><span class="a-num">【答案】{q.get("answer","")}</span><br>{ana}</div></li>')
    parts.append("</ol>")
    return "\n".join(parts)


# ===================== PDF 模板生成 =====================
def build_pdf_html(passage_results, all_words, config):
    readings = "\n".join(
        f'<div class="passage-block"><h2 class="passage-title">Text {i}</h2>'
        f'<div class="article">{article_to_html(r["article"])}</div>'
        f'<h3 class="questions-title">Questions</h3>'
        f'{questions_to_html(r["questions"])}</div><hr class="passage-divider">'
        for i, r in enumerate(passage_results, 1)
    )
    answers = "\n".join(
        f'<div class="answer-block"><h3 class="block-title text-success">Text {i} 答案与解析</h3>'
        f'{analysis_to_html(r["questions"])}</div>'
        for i, r in enumerate(passage_results, 1)
    )
    translations = "\n".join(
        f'<div class="translation-block"><h3 class="block-title text-purple">Text {i} 全文翻译</h3>'
        f'{translation_to_html(r["translation"])}</div>'
        for i, r in enumerate(passage_results, 1)
    )

    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<style>
@page {{ size: A4; margin: 1.8cm 2cm; }}
* {{ box-sizing: border-box; margin: 0; padding: 0; }}
body {{ font-family: 'Noto Serif CJK SC','Songti SC','SimSun',serif; font-size: 11pt; line-height: 1.9; color: #2c3e50; }}
.header {{ text-align: center; padding-bottom: 16px; border-bottom: 3px double #34495e; margin-bottom: 28px; }}
.header h1 {{ font-size: 19pt; color: #1a1a2e; margin: 0 0 6px 0; letter-spacing: 3px; }}
.header .meta {{ color: #7f8c8d; font-size: 9.5pt; }}
.passage-block {{ margin-bottom: 8px; }}
.passage-title {{ font-size: 14pt; color: #1a1a2e; border-left: 5px solid #3498db; padding-left: 12px; margin: 0 0 14px 0; }}
.article p {{ margin: 0 0 10px 0; text-align: justify; text-indent: 2em; }}
.article strong {{ color: #c0392b; font-weight: 700; }}
.questions-title {{ font-size: 12pt; color: #1a1a2e; border-left: 5px solid #e67e22; padding-left: 12px; margin: 0 0 14px 0; }}
.q-list {{ list-style: none; padding-left: 0; counter-reset: q-counter; }}
.q-item {{ margin-bottom: 16px; page-break-inside: avoid; counter-increment: q-counter; }}
.q-item::before {{ content: counter(q-counter) ". "; font-weight: 600; color: #3498db; }}
.q-stem {{ font-weight: 500; margin-bottom: 6px; text-align: justify; display: inline; }}
.opt-list {{ list-style: none; padding-left: 28px; margin: 4px 0 0 0; }}
.opt-list li {{ margin-bottom: 4px; text-align: justify; }}
.opt-letter {{ font-weight: 600; color: #7f8c8d; display: inline-block; width: 1.8em; }}
.passage-divider {{ border: none; border-top: 1px dashed #d0d0d0; margin: 28px 0; }}
.answer-section {{ page-break-before: always; }}
.answer-section > h2 {{ font-size: 16pt; color: #1a1a2e; border-bottom: 3px double #34495e; padding-bottom: 10px; margin: 0 0 24px 0; }}
.block-title {{ font-size: 13pt; color: #1a1a2e; padding-left: 12px; margin: 0 0 10px 0; }}
.text-success {{ border-left: 5px solid #2ecc71; }}
.text-purple {{ border-left: 5px solid #9b59b6; }}
.analysis-list {{ list-style: none; padding-left: 24px; counter-reset: a-counter; }}
.analysis-item {{ margin-bottom: 14px; padding: 12px 16px; background: #fafafa; border-left: 3px solid #bdc3c7; border-radius: 0 6px 6px 0; counter-increment: a-counter; }}
.analysis-item::before {{ content: counter(a-counter) ". "; font-weight: 700; color: #d35400; }}
.a-num {{ font-weight: 700; color: #d35400; font-size: 11pt; display: block; margin-bottom: 6px; }}
.a-content {{ font-size: 10pt; line-height: 1.85; color: #34495e; text-align: justify; }}
.a-content b {{ color: #2c3e50; }}
.a-content strong {{ color: #c0392b; }}
.translation-section {{ page-break-before: always; }}
.translation-section > h2 {{ font-size: 16pt; color: #1a1a2e; border-bottom: 3px double #34495e; padding-bottom: 10px; margin: 0 0 24px 0; }}
.translation-block {{ margin-bottom: 24px; }}
.translation-block p {{ margin: 0 0 8px 0; text-align: justify; text-indent: 2em; }}
.translation-block strong {{ color: #8e44ad; }}
.footer {{ text-align: center; margin-top: 36px; padding-top: 12px; border-top: 1px solid #eee; color: #bdc3c7; font-size: 8.5pt; }}
</style></head><body>
<div class="header">
    <h1>每日单词巩固阅读</h1>
    <div class="meta">日期：{TODAY} &nbsp;|&nbsp; 共 {len(all_words)} 个单词 &nbsp;|&nbsp; 难度：{config['name']}</div>
</div>
{readings}
<div class="answer-section"><h2>答案与解析</h2>{answers}</div>
<div class="translation-section"><h2>全文翻译</h2>{translations}</div>
<div class="footer">由墨墨背单词 + AI 自动生成</div>
</body></html>"""


def html_to_pdf(html_str):
    return HTML(string=html_str).write_pdf()


# ===================== 邮件发送 =====================
def send_email(pdf_bytes, to_email, config):
    subject = f"每日单词巩固阅读 - {TODAY}（{config['name']}）"
    msg = MIMEMultipart()
    msg["From"] = Header(f"墨墨单词助手 <{SMTP_USER}>")
    msg["To"] = Header(to_email)
    msg["Subject"] = Header(subject, "utf-8")
    msg.attach(MIMEText("今日单词巩固阅读 PDF 已生成，请查看附件。", "plain", "utf-8"))

    att = MIMEBase("application", "pdf")
    att.set_payload(pdf_bytes)
    encoders.encode_base64(att)
    att.add_header("Content-Disposition", "attachment", filename=("utf-8", "", f"每日单词巩固阅读-{TODAY}.pdf"))
    msg.attach(att)

    with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=15) as smtp:
        smtp.login(SMTP_USER, SMTP_PASS)
        smtp.sendmail(SMTP_USER, to_email, msg.as_string())


# ===================== 单用户流程 =====================
def process_user(user):
    token = user["maimemo_token"]
    email = user["email"]
    difficulty = user.get("difficulty", 2)
    config = DIFFICULTY_CONFIG[difficulty]
    tag = f"[{mask_email(email)}]"

    print(f"\n{'─'*60}")
    print(f"{tag} 🚀 开始处理 | 难度：{config['name']}")
    print(f"{'─'*60}")

    try:
        progress = get_today_progress(token)
        total = progress.get("total", 0)
        finished = progress.get("finished", 0)
        print(f"{tag} 📊 今日进度：{finished}/{total}")
    except Exception as e:
        print(f"{tag} ⚠️ 获取进度失败，将使用默认limit: {e}")
        total = 200

    try:
        words = get_today_words(token, limit=max(total, 1))
    except Exception as e:
        print(f"{tag} ❌ 获取单词失败: {e}")
        return
    if not words:
        print(f"{tag} ℹ️ 今日无单词，跳过")
        return
    print(f"{tag} ✅ 获取到 {len(words)} 个单词")

    groups = split_into_groups(words, 4)
    work_dir = tempfile.mkdtemp(prefix="momo_")
    try:
        results = []
        for idx, group in enumerate(groups, 1):
            print(f"{tag} 📝 第 {idx}/4 篇（{len(group)}词）")
            r = generate_passage(work_dir, group, idx, config, tag)
            results.append(r)

        print(f"{tag} 📄 生成PDF...")
        html = build_pdf_html(results, words, config)
        pdf = html_to_pdf(html)
        print(f"{tag} ✅ PDF生成完成 ({len(pdf)//1024}KB)")

        print(f"{tag} 📧 发送邮件...")
        send_email(pdf, email, config)
        print(f"{tag} ✅ 邮件发送成功！")
    except Exception as e:
        print(f"{tag} ❌ 处理失败: {e}")
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)
    
    print(f"{tag} 🏁 处理完成")


# ===================== 主流程 =====================
def main():
    users = load_users()
    if not users:
        print("❌ 未配置用户，请设置 USERS 环境变量或 MAIMEMO_TOKEN + EMAIL_TO")
        return

    print(f"\n{'═'*60}")
    print(f"🚀 墨墨背单词 - 每日阅读生成器")
    print(f"📅 日期：{TODAY}")
    print(f"👥 用户数：{len(users)}")
    print(f"⚙️ 并发数：{MAX_WORKERS}")
    print(f"{'═'*60}")

    if len(users) == 1:
        process_user(users[0])
    else:
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
            futures = {pool.submit(process_user, u): u["email"] for u in users}
            for future in as_completed(futures):
                email = futures[future]
                try:
                    future.result()
                except Exception as e:
                    print(f"[{mask_email(email)}] ❌ 线程异常: {e}")

    print(f"\n{'═'*60}")
    print("🎉 全部用户处理完成！")
    print(f"{'═'*60}\n")


if __name__ == "__main__":
    main()
