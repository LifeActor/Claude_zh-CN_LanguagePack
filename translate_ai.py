#!/usr/bin/env python3
"""
AI 翻译引擎
支持翻译：
  - translation-template/desktop-shell/zh-CN.json
  - translation-template/statsig/zh-CN.json
  - translation-template/ion-dist/parts/zh-CN.part-NNN.json
  - translation-template/ion-dist/zh-CN.overrides.json

只翻译 template 中有、但 translated-zh-CN 对应文件中没有的条目。
翻译结果直接写回 translated-zh-CN/ 对应文件（增量合并，不覆盖已有翻译）。

用法：
  python translate_ai.py desktop-shell
  python translate_ai.py statsig
  python translate_ai.py ion-dist 1          # 翻译 part-001
  python translate_ai.py ion-dist 1 5        # 翻译 part-001 到 part-005
  python translate_ai.py ion-dist all        # 翻译全部分片
  python translate_ai.py overrides           # 翻译 ion-dist overrides

  python translate_ai.py check desktop-shell # 检查译文质量
  python translate_ai.py check statsig
  python translate_ai.py check ion-dist
  python translate_ai.py check overrides
  python translate_ai.py check all           # 检查全部（同时写汇总日志）
  python translate_ai.py clean all           # 清理译文中已被新版模板移除的旧 key

  --force        强制重新翻译（覆盖已有译文）
  --workers N    并发线程数（默认 10）
  --batch-size N 每批翻译条数（默认 250，接口不稳时可降到 50）
  --fix          （仅 check）从译文中删除回退/纯英文条目，便于下次重译
  --log-dir DIR  （仅 check）日志输出目录（默认 check-reports/）
"""

import datetime
import json
import os
import re
import sys
import time
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from dotenv import load_dotenv
from openai import OpenAI

# ── 路径 ──────────────────────────────────────────────────────────────────────
ROOT = Path(__file__).parent
TEMPLATE_DIR = ROOT / "translation-template"
TRANSLATED_DIR = ROOT / "translated-zh-CN"
PARTS_DIR = TEMPLATE_DIR / "ion-dist" / "parts"
OVERRIDES_TEMPLATE = TEMPLATE_DIR / "ion-dist" / "zh-CN.overrides.json"
OVERRIDES_OUTPUT   = TRANSLATED_DIR / "ion-dist" / "zh-CN.overrides.json"

# ── 配置 ──────────────────────────────────────────────────────────────────────
load_dotenv(ROOT / ".env", override=True)

BATCH_SIZE = 250
DEFAULT_WORKERS = 10

# ── 系统提示词 ────────────────────────────────────────────────────────────────
SYSTEM_PROMPT = (
    "你是一名专业的软件本地化翻译员，负责将 Claude Desktop（Anthropic 出品的 AI 桌面客户端）"
    "的界面文案从英文翻译成简体中文。\n\n"

    "【产品背景】\n"
    "- 产品名称：Claude Desktop / Claude Cowork / Claude Code\n"
    "- 目标用户：中国大陆的开发者、AI 从业者、普通用户\n"
    "- 前端框架：Vue.js，i18n 使用 ICU MessageFormat\n\n"

    "【核心约束——违反任何一条均视为翻译失败】\n\n"

    "A. 严格的 key 一一对应\n"
    "   输入 JSON 中每个 key 对应一条独立的界面文案。\n"
    "   输出时，每个 key 的 value 必须且只能是该 key 对应原文的中文译文，\n"
    "   绝对不能将其他 key 的内容写入当前 key，也不能合并、拆分或跳过任何 key。\n"
    "   输出 JSON 的 key 集合必须与输入完全一致，不多不少。\n\n"

    "B. 译文长度与原文相称\n"
    "   - 原文是单词或短语（如 \"Export\"、\"Cancel\"）→ 译文同样是简短词语（\"导出\"、\"取消\"）\n"
    "   - 原文是一句话 → 译文是一句话\n"
    "   - 原文是段落 → 译文是段落\n"
    "   严禁将短标签翻译成长段落，也严禁将长段落压缩成单词。\n\n"

    "C. 译文内容必须忠实于原文\n"
    "   译文只能表达原文的含义，不得引入原文中没有的信息、话题或上下文。\n"
    "   如果译文内容与原文主题明显不符，说明发生了 key 错位，必须重新对应后再输出。\n\n"

    "【翻译规则】\n\n"

    "1. 占位符与标记——原样保留，不得修改：\n"
    "   - 花括号变量：{name}、{count}、{error}、{url} 等\n"
    "   - ICU 复数/选择语法：{count, plural, one {…} other {…}}、{gender, select, …}\n"
    "     其中 plural/select 关键字、one/other/male/female 等分支标签保持英文，\n"
    "     只翻译分支内的文案文字部分\n"
    "   - HTML/JSX 标签：<b>、<link>、<code>、<a>、<privacyLink> 等\n"
    "   - 转义字符：\\n、\\t 等\n"
    "   - 纯数字、单位、代码片段（如 {mA}mA、{kb}KB、{pct}%）\n\n"

    "2. 专有名词——保留英文，不翻译：\n"
    "   Claude、Claude Code、Claude Cowork、Anthropic、macOS、Windows、\n"
    "   BLE、API、JSON、UTF-8、OAuth、DXT、Statsig、Google Play、\n"
    "   Nordic UART Service 等品牌名、协议名、技术术语\n\n"

    "3. 翻译风格：\n"
    "   - 自然、口语化，符合中文产品习惯，避免机翻腔\n"
    "   - 界面按钮/标签用简短动词或名词（\"创建\"而非\"点击创建\"）\n"
    "   - 提示/说明句保持完整，语气友好\n"
    "   - 使用'你'而非'您'（产品风格偏年轻化）\n"
    "   - 标点使用中文全角（。？！，），但保留原文中的英文括号内内容不变\n\n"

    "【输入/输出格式】\n"
    "- 输入：JSON 对象，key 为不透明 ID，value 为英文原文\n"
    "- 输出：JSON 对象，key 与输入完全一致（数量、拼写均相同），value 为对应中文译文\n"
    "- 输出前自查：逐一确认每个 key 的译文内容与该 key 的原文语义一致、长度相称\n"
    "- 只输出 JSON，不加任何解释、注释或 markdown 代码块\n"
)

# 打印锁，避免多线程输出交错
_print_lock = threading.Lock()


def tprint(*args, **kwargs):
    with _print_lock:
        print(*args, **kwargs)


def get_client() -> tuple[OpenAI, str]:
    base_url = os.environ.get("OPENAI_BASE_URL")
    api_key = os.environ.get("OPENAI_API_KEY")
    model = os.environ.get("OPENAI_MODEL", "gpt-4o")
    if not base_url or not api_key:
        print("错误：请在 .env 中设置 OPENAI_BASE_URL 和 OPENAI_API_KEY")
        sys.exit(1)
    client = OpenAI(base_url=base_url, api_key=api_key)
    return client, model


def load_json(path: Path) -> dict:
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def save_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.write("\n")


def translate_batch(client: OpenAI, model: str, batch: dict, label: str) -> dict:
    user_payload = json.dumps(batch, ensure_ascii=False)
    for attempt in range(3):
        t0 = time.time()
        try:
            resp = client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": user_payload},
                ],
                temperature=0.2,
                response_format={"type": "json_object"},
            )
            content = resp.choices[0].message.content or ""
            result = json.loads(content)
            # 只保留输入中存在的 key，过滤模型多余输出
            return {k: result[k] for k in batch if k in result and isinstance(result[k], str)}
        except Exception as e:
            elapsed = time.time() - t0
            if attempt == 2:
                tprint(f"  [{label}] [警告] 翻译失败，使用原文回退: {e}")
                return batch
            wait = 2 ** attempt
            tprint(f"  [{label}] [重试 {attempt+1}/3] 失败（{elapsed:.1f}s），{wait}s 后重试: {e}")
            time.sleep(wait)
    return batch


def translate_concurrent(
    client: OpenAI,
    model: str,
    pending: dict,
    output_path: Path,
    file_lock: threading.Lock,
    label: str,
    workers: int,
) -> None:
    keys = list(pending.keys())
    batches = [
        {k: pending[k] for k in keys[i: i + BATCH_SIZE]}
        for i in range(0, len(keys), BATCH_SIZE)
    ]
    total = len(keys)
    completed_count = 0

    tprint(f"[{label}] 待翻译 {total} 条，分 {len(batches)} 批，并发 {workers} 线程...")

    t_start = time.time()

    def do_batch(idx: int, batch: dict) -> dict:
        return translate_batch(client, model, batch, label=f"{label} 批{idx+1}/{len(batches)}")

    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {executor.submit(do_batch, i, b): i for i, b in enumerate(batches)}
        for future in as_completed(futures):
            result = future.result()
            with file_lock:
                existing = load_json(output_path)
                existing.update(result)
                save_json(output_path, existing)
            completed_count += len(result)
            elapsed = time.time() - t_start
            tprint(f"  [{label}] 进度: {completed_count}/{total}，已用 {elapsed:.1f}s")

    total_elapsed = time.time() - t_start
    tprint(f"[{label}] 完成，总耗时 {total_elapsed:.1f}s → {output_path}")


def run_flat(client: OpenAI, model: str, name: str, force: bool, workers: int) -> None:
    template_path = TEMPLATE_DIR / name / "zh-CN.json"
    output_path = TRANSLATED_DIR / name / "zh-CN.json"
    template = load_json(template_path)
    existing = load_json(output_path)
    pending = dict(template) if force else {k: v for k, v in template.items() if k not in existing}

    if not pending:
        print(f"[{name}] 无新增条目，跳过。")
        return

    file_lock = threading.Lock()
    translate_concurrent(client, model, pending, output_path, file_lock, name, workers)


def run_parts(
    client: OpenAI,
    model: str,
    part_range: list[int] | None,
    force: bool,
    workers: int,
) -> None:
    part_files = sorted(PARTS_DIR.glob("zh-CN.part-*.json"))
    if not part_files:
        print("未找到分片文件，请先运行 split_template.py")
        return

    if part_range is None:
        targets = part_files
    else:
        start, end = part_range
        targets = [
            p for p in part_files
            if start <= int(re.search(r"part-(\d+)", p.name).group(1)) <= end
        ]

    if not targets:
        print("没有匹配的分片文件。")
        return

    output_path = TRANSLATED_DIR / "ion-dist" / "zh-CN.json"
    file_lock = threading.Lock()

    for part_path in targets:
        part_num = re.search(r"part-(\d+)", part_path.name).group(1)
        template = load_json(part_path)

        with file_lock:
            existing = load_json(output_path)

        pending = dict(template) if force else {k: v for k, v in template.items() if k not in existing}

        if not pending:
            print(f"[ion-dist/part-{part_num}] 无新增条目，跳过。")
            continue

        translate_concurrent(
            client, model, pending, output_path, file_lock,
            f"ion-dist/part-{part_num}", workers,
        )


def run_overrides(client: OpenAI, model: str, force: bool, workers: int) -> None:
    """翻译 ion-dist overrides 文件（条目数通常较少，单批完成）。"""
    if not OVERRIDES_TEMPLATE.exists():
        print("[overrides] 模板文件不存在，请先运行提取脚本生成 translation-template/ion-dist/zh-CN.overrides.json")
        return

    template = load_json(OVERRIDES_TEMPLATE)
    if not template:
        print("[overrides] 模板为空，跳过。")
        return

    existing = load_json(OVERRIDES_OUTPUT)
    pending = dict(template) if force else {k: v for k, v in template.items() if k not in existing}

    if not pending:
        print("[overrides] 无新增条目，跳过。")
        return

    file_lock = threading.Lock()
    translate_concurrent(client, model, pending, OVERRIDES_OUTPUT, file_lock, "overrides", workers)


def _has_chinese(s: str) -> bool:
    return any("一" <= ch <= "鿿" for ch in s)


def _has_letter(s: str) -> bool:
    return any(ch.isalpha() and ch.isascii() for ch in s)


def _strip_placeholders(s: str) -> str:
    """移除 ICU 占位符、HTML 标签、转义序列后返回纯文本，用于长度估算。
    使用括号计数处理嵌套花括号（如 ICU 分支内的 {label}）。
    """
    # 用括号计数移除所有 {...} 包括嵌套
    result = []
    depth = 0
    for ch in s:
        if ch == "{":
            depth += 1
        elif ch == "}":
            if depth > 0:
                depth -= 1
        elif depth == 0:
            result.append(ch)
    s = "".join(result)
    s = re.sub(r"<[^>]+>", "", s)
    s = re.sub(r"\\[ntr\\]", "", s)
    return s.strip()


# 不需要翻译的原文模式：URL、路径、邮箱、纯技术字符串
_UNTRANSLATABLE_RE = re.compile(
    r"^("
    r"https?://\S+"                              # URL
    r"|[~/][\w./\-]+"                            # Unix 路径
    r"|[A-Z]:\\[\w\\./\-]+"                      # Windows 路径
    r"|\S+@\S+\.\S+"                             # 邮箱
    r"|\*\.[A-Za-z0-9_.-]+"                      # 通配符域名
    r"|\*?\.?[\w\-]+(?:\.[\w\-]+)+(?:/[\w\-./]*)?"  # 域名/通配符域名/文件名
    r"|[A-Z0-9_\-]+(?:\s[A-Z0-9_\-]+)*"         # 全大写缩写序列
    r"|[A-Za-z][\w.-]*(?:/[A-Za-z0-9_.-]+)+"    # owner/repo、CLIENT_ID/.default 等
    r"|[A-Za-z]+(?:-[A-Za-z0-9]+)+"             # X-Header-Name、us-east-1 等
    r"|[A-Za-z]+(?:[A-Z][A-Za-z0-9]*)+"         # toolName、BedrockInference 等
    r"|[A-Za-z]+-\d+(?:\s\([^)]+\))?"           # Latin-1 (ISO-8859-1) 等
    r"|[+~]?\s*[A-Z]{2,}"                        # 税率标记（+ JCT 等）
    r"|©.+"                                      # 版权声明
    r"|\[\"[^\]]+\"\]"                           # JSON 数组字面量
    r"|[a-z]+://\S+(?:\s+\S+)*"                  # 自定义协议 URL（claude://...）
    r"|sk-[a-z]+-[…\w]+"                         # API key 示例
    r"|#[0-9A-Fa-f]{3,8}"                        # 颜色值
    r"|\d+\s*[xX×]\s*.+"                         # 数量×产品名
    r"|(?:Linux|Windows|macOS|Darwin|Intel)\s*\([^)]+\)"  # 平台名（Linux (x64) 等）
    r"|\d+\s+\w+\s+St,.+"                        # 街道地址
    r"|PO Box.+"                                 # 邮政信箱
    r")",
)

# 不需要翻译的完整词列表（品牌名、专有名词）
_BRAND_WORDS: frozenset[str] = frozenset({
    "claude", "anthropic", "cowork", "haiku", "sonnet", "opus",
    "google", "gmail", "github", "gitlab", "slack", "notion",
    "instagram", "linkedin", "reddit", "tiktok", "youtube", "canva",
    "excel", "powerpoint", "word", "outlook", "chrome", "safari",
    "windows", "macos", "linux", "android", "ios",
    "python", "bash", "shell", "sql", "json", "xml", "html", "css",
    "api", "url", "ssh", "ssl", "tls", "oauth", "scim", "saml",
    "mcp", "ghe", "aws", "gcp", "azure", "bedrock",
    "webhook", "webhooks", "opentelemetry",
    "pro", "free", "beta", "true", "false",
    "finder", "jetbrains", "vscode", "composer",
    "openid", "profile", "email", "offline", "access",  # OAuth scope 词
    "bearer", "option", "ctrl", "space", "esc",          # 键盘/认证词
    "latin", "cyrillic", "utf",                          # 编码名
    "value", "server", "name", "type", "status",         # 通用示例词
    "enterprise", "standard", "premium", "nonprofit",    # 套餐词（常与品牌组合）
    "labs", "research", "mini", "ship", "mythos",        # Claude 产品线
    "for", "in", "of", "the", "and", "or", "with", "on", "as", "user", "your",  # 品牌/技术组合中的虚词
    "remote", "cli", "code", "desktop", "artifact",
    "sales", "academy", "slack", "teams",
    "x", "ci", "dd", "mm", "mtd", "wau", "roi",         # 缩写/指标
    "ebitda", "fcf", "gst", "jct", "p", "b", "s", "d", "e",
    "v", "fps", "id", "ids",
    "prs", "ghe", "ble", "dxt",
    "conway", "clawdmart", "acme", "corp",               # 示例公司名
    "alex", "johnson",                                   # 示例人名
    "wireless", "headphones",                            # 示例产品名
    "mcp", "bash", "zsh",
    "play", "cloud", "kms", "app", "store", "amazon", "ai",
    "web", "services", "service", "key", "management",
    "vertex", "drive", "office", "agents", "bedrock",
    "max", "chat", "analytics", "workforce", "identity",
    "idp", "oidc", "sts", "vault", "uri", "audience",
    "foundry", "token", "bearer", "sans", "serif",
    "alt", "space", "microsoft", "azure", "mac", "apple",
    "silicon", "intel", "deb", "arm", "arm64", "x64",
    "payload", "logo", "markdown", "subagents", "session",
    "lens", "ultracode", "scrum", "master", "twitter",
    "git", "vs", "wow", "fable", "growthbook", "calendar",
    "apis", "youtuber", "latin", "gocspx", "nnn", "pr",
    "messages",
})


def _is_untranslatable(src: str) -> bool:
    """判断原文是否本身就不需要翻译（品牌名、技术词、URL、路径等）。"""
    stripped = _strip_placeholders(src).strip()

    # 去掉占位符后为空 → 纯占位符/ICU 模板，不需翻译
    if not stripped:
        return True

    # 正则快速匹配
    if _UNTRANSLATABLE_RE.fullmatch(stripped):
        return True

    # 去掉标点/数字/空格后，所有词都是品牌/技术词
    words = re.findall(r"[a-zA-Z]+", stripped.lower())
    if words and all(w in _BRAND_WORDS for w in words):
        return True

    # 含多个空格分隔的 token，且每个 token 都是 URL/路径/技术词
    # 处理 OAuth scope 字符串：'openid email https://...'
    tokens = stripped.split()
    if len(tokens) >= 2 and all(re.fullmatch(r"[A-Za-z0-9_./-]+", t) for t in tokens) and any(
        any(ch in t for ch in "_/.") for t in tokens
    ):
        return True

    if len(tokens) >= 2 and all(
        re.match(r"https?://\S+", t) or re.match(r"[\w\-]+(?:\.[\w\-]+)+", t)
        or re.match(r"[a-zA-Z_]+", t) and t.lower() in _BRAND_WORDS
        for t in tokens
    ):
        return True

    return False


def _length_ratio(src: str, tgt: str) -> float:
    """译文字符数 / 原文字符数（去除占位符后）。"""
    src_clean = _strip_placeholders(src)
    tgt_clean = _strip_placeholders(tgt)
    if not src_clean:
        return 1.0
    return len(tgt_clean) / max(len(src_clean), 1)


# 长度比超过此阈值视为"译文异常膨胀"
_LENGTH_RATIO_MAX = 5.0
# 原文去占位符后字符数超过此值才做膨胀检查（短标签误报率高）
_LENGTH_CHECK_MIN_SRC = 8

# ── 深校辅助 ──────────────────────────────────────────────────────────────────

_ICU_RE = re.compile(r"\{(\w+)\s*,\s*(?:plural|select|selectordinal)\b")


def _has_icu(s: str) -> bool:
    return bool(_ICU_RE.search(s))


def _extract_icu_vars(s: str) -> set[str]:
    return set(_ICU_RE.findall(s))


def _strip_icu_branches(s: str) -> str:
    """将 ICU 分支内容替换为空，只保留顶层结构，用于提取顶层简单变量。
    例: '{count, plural, one {# item} other {# items}}' -> '{count, plural, one {} other {}}'
    采用括号计数处理嵌套花括号。
    """
    result = []
    depth = 0
    in_icu = False
    i = 0
    while i < len(s):
        ch = s[i]
        if ch == "{":
            depth += 1
            result.append(ch)
            # 检查是否是 ICU 语法块（{word, plural/select/...）
            if depth == 1:
                m = _ICU_RE.match(s, i)
                if m:
                    in_icu = True
        elif ch == "}":
            if in_icu and depth == 1:
                in_icu = False
            depth -= 1
            result.append(ch)
        elif in_icu and depth >= 2:
            # ICU 分支内容，跳过（用空格占位保持长度不变不重要，直接跳过）
            pass
        else:
            result.append(ch)
        i += 1
    return "".join(result)


def _extract_simple_vars(s: str) -> set[str]:
    """提取顶层 {word} 简单变量，排除 ICU 分支内的花括号内容。"""
    top = _strip_icu_branches(s)
    # 匹配 {word}，不含逗号（排除 ICU 语法头部）
    return set(re.findall(r"\{(\w+)\}", top))


def _extract_html_tags(s: str) -> set[str]:
    return set(re.findall(r"</?(\w[\w-]*)", s))


def _extract_escapes(s: str) -> list[str]:
    return sorted(re.findall(r"\\[ntr\\]", s))


def _deep_check(src: str, tgt: str) -> list[str]:
    """返回深校问题描述列表，空列表表示无问题。"""
    issues: list[str] = []

    src_icu, tgt_icu = _has_icu(src), _has_icu(tgt)
    if not src_icu and tgt_icu:
        issues.append("原文无 ICU 语法，译文含 ICU 语法（疑似 key 错位）")
    elif src_icu and not tgt_icu:
        issues.append("原文含 ICU 语法，译文无 ICU 语法（ICU 结构丢失）")
    elif src_icu and tgt_icu:
        sv, tv = _extract_icu_vars(src), _extract_icu_vars(tgt)
        if sv != tv:
            issues.append(f"ICU 变量名不匹配: 原文 {sorted(sv)}，译文 {sorted(tv)}")

    src_vars, tgt_vars = _extract_simple_vars(src), _extract_simple_vars(tgt)
    lost = src_vars - tgt_vars
    extra = tgt_vars - src_vars
    if lost:
        issues.append(f"占位符丢失: {', '.join('{' + v + '}' for v in sorted(lost))}")
    if extra:
        issues.append(f"占位符多余: {', '.join('{' + v + '}' for v in sorted(extra))}")

    src_tags, tgt_tags = _extract_html_tags(src), _extract_html_tags(tgt)
    lost_tags = src_tags - tgt_tags
    extra_tags = tgt_tags - src_tags
    if lost_tags:
        issues.append(f"HTML 标签丢失: {', '.join('<' + t + '>' for t in sorted(lost_tags))}")
    if extra_tags:
        issues.append(f"HTML 标签多余: {', '.join('<' + t + '>' for t in sorted(extra_tags))}")

    src_esc, tgt_esc = _extract_escapes(src), _extract_escapes(tgt)
    if src_esc != tgt_esc:
        issues.append(f"转义序列不匹配: 原文 {src_esc}，译文 {tgt_esc}")

    if src.strip() and not tgt.strip():
        issues.append("译文为空")

    return issues


def _write_check_log(
    log_dir: Path,
    name: str,
    total: int,
    translated_count: int,
    fallback: list,
    missing: list,
    english_only: list,
    bloated: list,
    deep_errors: list,
) -> None:
    log_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    log_path = log_dir / f"{ts}-{name.replace('/', '-')}.json"

    report: dict = {
        "generated_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "target": name,
        "summary": {
            "template_total": total,
            "translated_count": translated_count,
            "issues_total": len(fallback) + len(missing) + len(english_only) + len(bloated) + len(deep_errors),
            "fallback": len(fallback),
            "missing": len(missing),
            "english_only": len(english_only),
            "bloated": len(bloated),
            "deep_errors": len(deep_errors),
        },
        "fallback": [
            {"key": k, "src": src, "tgt": tgt}
            for k, src, tgt in fallback
        ],
        "missing": [
            {"key": k, "src": src}
            for k, src in missing
        ],
        "english_only": [
            {"key": k, "src": src, "tgt": tgt}
            for k, src, tgt in english_only
        ],
        "bloated": [
            {"key": k, "src": src, "tgt": tgt, "ratio": round(ratio, 2)}
            for k, src, tgt, ratio in bloated
        ],
        "deep_errors": [
            {"key": k, "src": src, "tgt": tgt, "issues": issues}
            for k, src, tgt, issues in deep_errors
        ],
    }

    with log_path.open("w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
        f.write("\n")

    print(f"  [日志] 已写入 {log_path}")


def _load_template_and_output(name: str) -> tuple[dict, Path]:
    """返回指定目标的当前模板和译文输出路径。"""
    if name == "ion-dist":
        template_path = TEMPLATE_DIR / "ion-dist" / "zh-CN.json"
        if template_path.exists():
            template = load_json(template_path)
        else:
            template = {}
            for p in sorted(PARTS_DIR.glob("zh-CN.part-*.json")):
                template.update(load_json(p))
        output_path = TRANSLATED_DIR / "ion-dist" / "zh-CN.json"
    elif name == "overrides":
        template = load_json(OVERRIDES_TEMPLATE)
        output_path = OVERRIDES_OUTPUT
    else:
        template = load_json(TEMPLATE_DIR / name / "zh-CN.json")
        output_path = TRANSLATED_DIR / name / "zh-CN.json"

    return template, output_path


def clean_obsolete_keys(name: str) -> int:
    """删除译文中存在、但当前模板已不存在的旧 key，返回删除数量。"""
    template, output_path = _load_template_and_output(name)
    translated = load_json(output_path)

    obsolete_keys = sorted(set(translated) - set(template))
    if not obsolete_keys:
        print(f"[{name}] 无废弃 key，跳过。")
        return 0

    cleaned = {k: v for k, v in translated.items() if k in template}
    save_json(output_path, cleaned)

    print(f"[{name}] 已清理 {len(obsolete_keys)} 个废弃 key → {output_path}")
    print(f"  示例: {', '.join(obsolete_keys[:5])}")
    return len(obsolete_keys)


def check_translation(
    name: str,
    fix: bool = False,
    log_dir: Path | None = None,
) -> tuple[int, int, int, int, int]:
    """检查译文质量，返回 (回退数, 缺失数, 纯英文数, 膨胀数, 深校错误数)。"""
    template, output_path = _load_template_and_output(name)
    translated = load_json(output_path)

    fallback:     list[tuple[str, str, str]] = []
    missing:      list[tuple[str, str]]      = []
    english_only: list[tuple[str, str, str]] = []
    bloated:      list[tuple[str, str, str, float]] = []
    deep_errors:  list[tuple[str, str, str, list[str]]] = []

    for k, src in template.items():
        if not isinstance(src, str):
            continue
        if k not in translated:
            missing.append((k, src))
            continue
        tgt = translated[k]
        if not isinstance(tgt, str):
            continue

        src_clean = _strip_placeholders(src)
        flagged = False

        if not _has_letter(src_clean):
            # 原文去占位符后无实质字母，仅做结构深校
            di = _deep_check(src, tgt)
            if di:
                deep_errors.append((k, src, tgt, di))
            continue

        # 原文本身不需要翻译（品牌名、URL、路径、纯技术词等），跳过所有检查
        if _is_untranslatable(src):
            continue

        if tgt == src:
            fallback.append((k, src, tgt))
            flagged = True

        if not flagged and _has_letter(tgt) and not _has_chinese(tgt):
            english_only.append((k, src, tgt))
            flagged = True

        if not flagged and len(src_clean) >= _LENGTH_CHECK_MIN_SRC:
            ratio = _length_ratio(src, tgt)
            if ratio > _LENGTH_RATIO_MAX:
                bloated.append((k, src, tgt, ratio))
                flagged = True

        if not flagged:
            di = _deep_check(src, tgt)
            if di:
                deep_errors.append((k, src, tgt, di))

    total = len(template)
    issues = len(fallback) + len(missing) + len(english_only) + len(bloated) + len(deep_errors)

    print(f"\n[{name}] 模板 {total} 条，译文 {len(translated)} 条，问题 {issues} 条")
    print(f"  回退条目  (译文==原文)              : {len(fallback)}")
    print(f"  缺失条目  (译文中没有)              : {len(missing)}")
    print(f"  纯英文条目(含字母但无中文)          : {len(english_only)}")
    print(f"  膨胀条目  (译文长度>原文×{_LENGTH_RATIO_MAX:.0f})      : {len(bloated)}")
    print(f"  深校错误  (结构/占位符/ICU 不一致)  : {len(deep_errors)}")

    def _fmt(v: str, limit: int = 90) -> str:
        return v if len(v) <= limit else v[:limit - 3] + "..."

    def _preview_3(items: list[tuple[str, str, str]], title: str) -> None:
        if not items:
            return
        print(f"  ── {title} 示例（前 {min(3, len(items))} 条）──")
        for k, src, tgt in items[:3]:
            print(f"    key : {k}")
            print(f"    原文: {_fmt(src)}")
            print(f"    译文: {_fmt(tgt)}")

    def _preview_bloated(items: list[tuple[str, str, str, float]]) -> None:
        if not items:
            return
        print(f"  ── 膨胀条目示例（前 {min(3, len(items))} 条）──")
        for k, src, tgt, ratio in items[:3]:
            print(f"    key : {k}  (×{ratio:.1f})")
            print(f"    原文: {_fmt(src)}")
            print(f"    译文: {_fmt(tgt)}")

    def _preview_deep(items: list[tuple[str, str, str, list[str]]]) -> None:
        if not items:
            return
        print(f"  ── 深校错误示例（前 {min(3, len(items))} 条）──")
        for k, src, tgt, di in items[:3]:
            print(f"    key : {k}")
            print(f"    原文: {_fmt(src)}")
            print(f"    译文: {_fmt(tgt)}")
            for d in di:
                print(f"    -   {d}")

    _preview_3(fallback, "回退")
    _preview_3(english_only, "纯英文")
    _preview_bloated(bloated)
    _preview_deep(deep_errors)
    if missing:
        print(f"  ── 缺失条目示例（前 {min(3, len(missing))} 条）──")
        for k, src in missing[:3]:
            print(f"    key : {k}")
            print(f"    原文: {_fmt(src)}")

    if fix:
        bad_keys: dict[str, str] = {}
        for k, _, _ in fallback:
            bad_keys[k] = "回退"
        for k, _, _ in english_only:
            bad_keys[k] = "纯英文"
        for k, _, _, _ in bloated:
            bad_keys.setdefault(k, "膨胀")
        for k, _, _, _ in deep_errors:
            bad_keys.setdefault(k, "深校错误")

        if bad_keys:
            cleaned = {k: v for k, v in translated.items() if k not in bad_keys}
            save_json(output_path, cleaned)
            by_type: dict[str, int] = {}
            for t in bad_keys.values():
                by_type[t] = by_type.get(t, 0) + 1
            summary = "、".join(f"{t} {n} 条" for t, n in by_type.items())
            print(f"\n  [--fix] 已删除 {len(bad_keys)} 条问题译文（{summary}），下次翻译时将重新生成。")
        else:
            print("\n  [--fix] 无需修复。")

    if log_dir is not None:
        _write_check_log(
            log_dir=log_dir,
            name=name,
            total=total,
            translated_count=len(translated),
            fallback=fallback,
            missing=missing,
            english_only=english_only,
            bloated=bloated,
            deep_errors=deep_errors,
        )

    return len(fallback), len(missing), len(english_only), len(bloated), len(deep_errors)


def main() -> None:
    global BATCH_SIZE

    args = sys.argv[1:]
    if not args:
        print(__doc__)
        sys.exit(1)

    force = "--force" in args
    args = [a for a in args if a != "--force"]

    fix = "--fix" in args
    args = [a for a in args if a != "--fix"]

    log_dir: Path | None = ROOT / "check-reports"
    if "--log-dir" in args:
        idx = args.index("--log-dir")
        if idx + 1 >= len(args):
            print("--log-dir 需要指定目录，例如 --log-dir ./reports")
            sys.exit(1)
        log_dir = Path(args[idx + 1])
        args = args[:idx] + args[idx + 2:]
    if "--no-log" in args:
        log_dir = None
        args = [a for a in args if a != "--no-log"]

    workers = DEFAULT_WORKERS
    if "--workers" in args:
        idx = args.index("--workers")
        if idx + 1 >= len(args):
            print("--workers 需要指定数量，例如 --workers 10")
            sys.exit(1)
        workers = int(args[idx + 1])
        args = args[:idx] + args[idx + 2:]

    if "--batch-size" in args:
        idx = args.index("--batch-size")
        if idx + 1 >= len(args):
            print("--batch-size 需要指定数量，例如 --batch-size 50")
            sys.exit(1)
        BATCH_SIZE = max(1, int(args[idx + 1]))
        args = args[:idx] + args[idx + 2:]

    target = args[0].lower()

    # check 子命令：不需要 API 客户端
    if target == "check":
        if len(args) < 2:
            print("用法: python translate_ai.py check <desktop-shell|statsig|ion-dist|all> [--fix]")
            sys.exit(1)
        sub = args[1].lower()
        targets = ["desktop-shell", "statsig", "ion-dist", "overrides"] if sub == "all" else [sub]
        totals = [0, 0, 0, 0, 0]
        for t in targets:
            if t not in ("desktop-shell", "statsig", "ion-dist", "overrides"):
                print(f"未知检查目标: {t}")
                sys.exit(1)
            a, b, c, d, e = check_translation(t, fix=fix, log_dir=log_dir)
            totals[0] += a; totals[1] += b; totals[2] += c; totals[3] += d; totals[4] += e
        if len(targets) > 1:
            print(f"\n[汇总] 回退 {totals[0]}，缺失 {totals[1]}，纯英文 {totals[2]}，膨胀 {totals[3]}，深校错误 {totals[4]}")
            if log_dir is not None:
                ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
                summary_path = log_dir / f"{ts}-summary.json"
                log_dir.mkdir(parents=True, exist_ok=True)
                summary = {
                    "generated_at": datetime.datetime.now().isoformat(timespec="seconds"),
                    "targets": targets,
                    "totals": {
                        "fallback": totals[0],
                        "missing": totals[1],
                        "english_only": totals[2],
                        "bloated": totals[3],
                        "deep_errors": totals[4],
                        "issues_total": sum(totals),
                    },
                }
                with summary_path.open("w", encoding="utf-8") as f:
                    json.dump(summary, f, ensure_ascii=False, indent=2)
                    f.write("\n")
                print(f"[汇总日志] 已写入 {summary_path}")
        return

    # clean 子命令：不需要 API 客户端，用于安装前清理旧版本残留 key
    if target == "clean":
        if len(args) < 2:
            print("用法: python translate_ai.py clean <desktop-shell|statsig|ion-dist|overrides|all>")
            sys.exit(1)
        sub = args[1].lower()
        targets = ["desktop-shell", "statsig", "ion-dist", "overrides"] if sub == "all" else [sub]
        total_removed = 0
        for t in targets:
            if t not in ("desktop-shell", "statsig", "ion-dist", "overrides"):
                print(f"未知清理目标: {t}")
                sys.exit(1)
            total_removed += clean_obsolete_keys(t)
        if len(targets) > 1:
            print(f"\n[汇总] 已清理 {total_removed} 个废弃 key")
        return

    client, model = get_client()

    if target in ("desktop-shell", "statsig"):
        run_flat(client, model, target, force=force, workers=workers)

    elif target == "overrides":
        run_overrides(client, model, force=force, workers=workers)

    elif target == "ion-dist":
        if len(args) < 2:
            print("用法: python translate_ai.py ion-dist <part编号|all> [结束编号] [--force] [--workers N]")
            sys.exit(1)

        if args[1].lower() == "all":
            run_parts(client, model, None, force=force, workers=workers)
        else:
            start = int(args[1])
            end = int(args[2]) if len(args) >= 3 else start
            run_parts(client, model, [start, end], force=force, workers=workers)

    else:
        print(f"未知目标: {target}")
        print(__doc__)
        sys.exit(1)


if __name__ == "__main__":
    main()
