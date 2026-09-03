"""Git 断言工具箱（docs/dev/10 第 3 节）。

工具箱是一个**外部独立仓库**（`skill-evaluate-assertion-toolbox`），结构约定见
设计文档第 3.1 节：`templates/` 放模板，`manifest.yaml` 放每个模板的元数据。
本模块负责三件事：

1. **同步**：把仓库 `git clone/fetch` 到本地缓存目录，并解析出当前 commit sha
   （写进 `AssertionSpec.template_ref`，保证断点恢复后模板版本不漂移）。
2. **检索**：`lookup()` 按关键词打分选模板。当前是关键词匹配版；`_semantic_lookup()`
   是留给 docs/dev/23 的占位钩子（向量 + BM25 + Reranker 混合检索）。
3. **渲染**：`.jinja` 模板按参数渲染成可执行脚本；非 `.jinja` 模板（如
   `sql_no_injection_validator.py`）原样取用。

**工具箱不可用不是错误**：仓库没配、没拉下来、manifest 缺失，`available` 为
False，`lookup()` 返回空列表，`ValidatorAgent` 据此走 `generated_from_scratch`。
工具箱是加速与规范化手段，不是运行前置条件。
"""

from __future__ import annotations

import asyncio
import re
import subprocess
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from jinja2 import StrictUndefined, Template

from skill_evaluate.config import get_settings
from skill_evaluate.errors import AgentError
from skill_evaluate.logging import get_logger

logger = get_logger(component="assertion_toolbox")

MANIFEST_FILENAME = "manifest.yaml"
TEMPLATES_DIRNAME = "templates"
UNKNOWN_REF = "unknown"

# 关键词抽取时丢弃的高频无信息词。刻意保持极小：这是一个打分器，不是分词器，
# 停用词表膨胀反而会让"删除文件"这类真正的动词被误删。
_STOPWORDS = frozenset(
    {
        "the",
        "a",
        "an",
        "and",
        "or",
        "of",
        "to",
        "in",
        "is",
        "are",
        "be",
        "for",
        "with",
        "that",
        "this",
        "it",
        "on",
        "as",
        "at",
        "by",
        "from",
        "should",
        "must",
        "will",
        "请",
        "一个",
        "然后",
        "并且",
        "以及",
        "如果",
        "可以",
        "需要",
    }
)

_ASCII_TOKEN_RE = re.compile(r"[a-z0-9_]{2,}")
_CJK_RUN_RE = re.compile(r"[一-鿿]+")


class ToolboxError(AgentError):
    """工具箱结构非法（manifest 解析失败、模板文件缺失等）。

    与"工具箱不可用"区分开：不可用是合法状态（走 generated_from_scratch），
    而**结构非法**说明仓库被改坏了，静默降级会让所有用例悄悄失去模板复用，
    因此显式报错。
    """


# --------------------------------------------------------------------------- #
# 元数据
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class TemplateMetadata:
    """`manifest.yaml` 中的一条记录。"""

    template: str
    description: str
    keywords: tuple[str, ...]
    params: tuple[str, ...]
    language: str = "python"

    @property
    def is_jinja(self) -> bool:
        return self.template.endswith(".jinja")


@dataclass(frozen=True, slots=True)
class TemplateMatch:
    template: TemplateMetadata
    score: float  # 0~1，命中的 keywords 占比


# --------------------------------------------------------------------------- #
# 关键词抽取与打分
# --------------------------------------------------------------------------- #


def extract_keywords(text: str) -> set[str]:
    """从任意文本抽取用于匹配的 token 集合。

    中英混排必须都能处理：英文按单词切；中文没有空格，按 2-gram 切（`格式校验`
    -> `格式`/`式校`/`校验`），这样 manifest 里写 `格式校验`、用例里写
    `校验格式` 也能对上。项目不引入分词依赖——一个打分器不值得为此增加一个
    需要下载词典的运行期依赖。
    """
    lowered = text.lower()
    tokens = {t for t in _ASCII_TOKEN_RE.findall(lowered) if t not in _STOPWORDS}
    for run in _CJK_RUN_RE.findall(lowered):
        if len(run) <= 2:
            tokens.add(run)
            continue
        tokens.update(run[i : i + 2] for i in range(len(run) - 1))
    return tokens


def score_template(meta: TemplateMetadata, query_text: str) -> float:
    """模板与查询文本的匹配分：命中的 keyword 数 / keyword 总数。

    单个 keyword 命中的判定有两条（满足其一即可）：整词出现在查询原文里（子串），
    或它的 token 与查询 token 有交集。前者精确、后者宽松，两者结合避免"中文
    2-gram 拆得太碎导致什么都能命中"和"完全按整词匹配导致什么都命不中"两个极端。
    """
    if not meta.keywords:
        return 0.0
    lowered = query_text.lower()
    query_tokens = extract_keywords(query_text)
    hits = 0
    for keyword in meta.keywords:
        key = keyword.lower().strip()
        if not key:
            continue
        if key in lowered or extract_keywords(key) & query_tokens:
            hits += 1
    return hits / len(meta.keywords)


# --------------------------------------------------------------------------- #
# manifest 解析
# --------------------------------------------------------------------------- #


def parse_manifest(raw: str) -> list[TemplateMetadata]:
    """解析 `manifest.yaml`。

    优先用 PyYAML（装了就用），否则退化为一个只认设计文档第 3.1 节那种形状的
    极简解析器——与 `ingestion/skill_loader.py` 解析 frontmatter 的取舍一致：
    为一个固定形状的小文件引入一个运行期依赖不划算，但装了更好。
    """
    try:  # pragma: no cover - 取决于环境是否装了 PyYAML
        import yaml
    except ImportError:
        records = _parse_manifest_minimal(raw)
    else:
        loaded = yaml.safe_load(raw)
        if loaded is None:
            records = []
        elif not isinstance(loaded, list):
            raise ToolboxError(f"{MANIFEST_FILENAME} 顶层必须是记录列表，实际是 {type(loaded)}")
        records = [dict(item) for item in loaded]

    return [_to_metadata(record) for record in records]


def _to_metadata(record: dict[str, Any]) -> TemplateMetadata:
    template = str(record.get("template", "")).strip()
    if not template:
        raise ToolboxError(f"{MANIFEST_FILENAME} 中存在缺少 `template` 字段的记录：{record!r}")
    language = str(record.get("language") or _infer_language(template))
    return TemplateMetadata(
        template=template,
        description=str(record.get("description", "")),
        keywords=tuple(str(k) for k in record.get("keywords", []) or []),
        params=tuple(str(p) for p in record.get("params", []) or []),
        language=language,
    )


def _infer_language(template_name: str) -> str:
    stem = template_name.removesuffix(".jinja")
    if stem.endswith((".sh", ".bash")):
        return "bash"
    return "python"


def _parse_manifest_minimal(raw: str) -> list[dict[str, Any]]:
    """极简 YAML 子集解析：`- key: value` 记录列表，值支持标量、行内列表、块列表。

    靠**缩进**区分"新记录"与"块列表项"：记录的 `- ` 位于最外层缩进（由第一条
    记录确定），块列表项一定比它更深。不这么做的话，`- template: b` 紧跟在
    `params:` 的块列表后面时会被误吞成上一条记录的列表项。
    """
    records: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    pending_list_key: str | None = None
    record_indent: int | None = None

    for line_no, line in enumerate(raw.splitlines(), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        indent = len(line) - len(line.lstrip())

        if stripped.startswith("- "):
            if record_indent is None:
                record_indent = indent
            if indent <= record_indent:  # 新记录
                current = {}
                records.append(current)
                pending_list_key = None
                stripped = stripped[2:].strip()
                record_indent = indent
            else:  # 块列表项
                if current is None or pending_list_key is None:
                    raise ToolboxError(f"{MANIFEST_FILENAME} 第 {line_no} 行：列表项没有归属的键")
                current[pending_list_key].append(_scalar(stripped[2:].strip()))
                continue

        if current is None:
            raise ToolboxError(f"{MANIFEST_FILENAME} 第 {line_no} 行：记录必须以 `- ` 开头")
        if ":" not in stripped:
            raise ToolboxError(f"{MANIFEST_FILENAME} 第 {line_no} 行无法解析：{line!r}")

        key, _, value = stripped.partition(":")
        key = key.strip()
        value = value.strip()
        if not value:  # 块列表的起始行：`keywords:`
            current[key] = []
            pending_list_key = key
        elif value.startswith("[") and value.endswith("]"):
            current[key] = [_scalar(v) for v in value[1:-1].split(",") if v.strip()]
            pending_list_key = None
        else:
            current[key] = _scalar(value)
            pending_list_key = None

    return records


def _scalar(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        return value[1:-1]
    return value


# --------------------------------------------------------------------------- #
# 工具箱
# --------------------------------------------------------------------------- #


class AssertionToolbox:
    """本地缓存的断言模板仓库视图。

    构造不做任何 IO（不 clone、不读文件），首次访问时才惰性加载——只做单测/静态
    审查的调用方不该因为"本地没有工具箱仓库"而无法实例化 `ValidatorAgent`。
    """

    def __init__(
        self,
        root: Path | str | None = None,
        *,
        repo_url: str | None = None,
        ref: str | None = None,
    ) -> None:
        settings = get_settings().validator
        self._root = (
            Path(root).expanduser() if root else Path(settings.toolbox_cache_dir).expanduser()
        )
        self._repo_url = repo_url if repo_url is not None else settings.toolbox_repo_url
        self._ref = ref or settings.toolbox_ref
        self._manifest: list[TemplateMetadata] | None = None
        self._commit_sha: str | None = None

    # ---------------------------------------------------------------- #
    # 基本属性
    # ---------------------------------------------------------------- #

    @property
    def root(self) -> Path:
        return self._root

    @property
    def manifest_path(self) -> Path:
        return self._root / MANIFEST_FILENAME

    @property
    def available(self) -> bool:
        return self.manifest_path.is_file()

    def manifest(self) -> list[TemplateMetadata]:
        if self._manifest is None:
            if not self.available:
                logger.info(
                    "assertion_toolbox_unavailable",
                    root=str(self._root),
                    hint="未同步或仓库未创建；Validator 将走 generated_from_scratch",
                )
                self._manifest = []
            else:
                self._manifest = parse_manifest(self.manifest_path.read_text(encoding="utf-8"))
        return self._manifest

    def commit_sha(self) -> str:
        """当前缓存目录的 commit sha；非 git 目录返回 `unknown`。"""
        if self._commit_sha is None:
            self._commit_sha = _git_head_sha(self._root)
        return self._commit_sha

    def template_ref(self, template_name: str) -> str:
        """写进 `AssertionSpec.template_ref` 的引用串：`templates/x.py.jinja@<sha>`。

        带上 sha 是为了让"这条断言当时用的是哪一版模板"可回溯：工具箱是外部仓库、
        会持续演进，只记路径的话，断点恢复或事后复盘时读到的模板可能已经不是当时
        那一份了（设计文档第 3.3 节）。
        """
        return f"{TEMPLATES_DIRNAME}/{template_name}@{self.commit_sha()}"

    # ---------------------------------------------------------------- #
    # 检索
    # ---------------------------------------------------------------- #

    async def lookup(self, query: str, *, threshold: float | None = None) -> list[TemplateMatch]:
        """按查询文本返回过阈值的模板，分数降序。"""
        limit = (
            get_settings().validator.template_match_threshold if threshold is None else threshold
        )
        matches = await self._semantic_lookup(query)
        return [m for m in matches if m.score >= limit]

    async def _semantic_lookup(self, query: str) -> list[TemplateMatch]:
        """占位：当前仅调用关键词匹配版本；docs/dev/23 接入后替换为混合检索实现。

        签名与返回类型是契约的一部分（docs/dev/10 第 3.2 节），23 号文档只替换
        函数体：向量语义检索 + BM25 + Reranker，`score` 归一化到同一个 0~1 区间，
        `ValidatorAgent` 与阈值配置都不需要改。
        """
        return self._keyword_lookup(query)

    def _keyword_lookup(self, query: str) -> list[TemplateMatch]:
        matches = [
            TemplateMatch(template=meta, score=score_template(meta, query))
            for meta in self.manifest()
        ]
        matches = [m for m in matches if m.score > 0]
        matches.sort(key=lambda m: (-m.score, m.template.template))
        return matches

    # ---------------------------------------------------------------- #
    # 读取与渲染
    # ---------------------------------------------------------------- #

    def read_template(self, template_name: str) -> str:
        path = self._root / TEMPLATES_DIRNAME / template_name
        if not path.is_file():
            raise ToolboxError(f"manifest 声明的模板文件不存在：{path}")
        return path.read_text(encoding="utf-8")

    def render(self, meta: TemplateMetadata, params: dict[str, str]) -> str:
        """渲染出可直接下发沙箱的脚本正文。

        非 `.jinja` 模板（如 `sql_no_injection_validator.py`）原样返回：它们是完整
        的独立脚本，参数化靠脚本自身的 CLI 参数或环境变量，不经模板引擎。
        """
        source = self.read_template(meta.template)
        if not meta.is_jinja:
            return source
        missing = [p for p in meta.params if p not in params]
        if missing:
            # StrictUndefined 也会报错，但那是渲染中途抛出的 UndefinedError，
            # 信息里只有第一个缺失变量。这里一次列全，调用方才知道要补什么。
            raise ToolboxError(
                f"模板 {meta.template} 缺少必需参数：{missing}（manifest 声明：{list(meta.params)}）"
            )
        # str(...)：jinja2 的 Template 构造被标注为返回 Any（元类 __new__），
        # 不显式收窄会让 strict 模式报 no-any-return。
        return str(
            Template(source, undefined=StrictUndefined, keep_trailing_newline=True).render(**params)
        )

    # ---------------------------------------------------------------- #
    # 同步（docs/dev/10 第 3.3 节）
    # ---------------------------------------------------------------- #

    async def sync(self) -> bool:
        """把工具箱仓库同步到本地缓存目录。返回是否同步成功。

        由 CI 定时任务或流水线入口按需调用，**不在 `lookup()` 内部隐式触发**——
        评测过程中途去拉一次外部仓库会让"这次评测用的是哪一版模板"变得不确定，
        也会把一次网络故障变成一次评测失败。
        """
        if not self._repo_url:
            logger.info("assertion_toolbox_sync_skipped", reason="toolbox_repo_url 未配置")
            return False
        try:
            await asyncio.to_thread(_git_sync, self._repo_url, self._ref, self._root)
        except (subprocess.CalledProcessError, OSError) as exc:
            # 同步失败降级为"工具箱不可用"，不中断评测（第 3.3 节：工具箱是加速手段）。
            logger.warning(
                "assertion_toolbox_sync_failed", repo=self._repo_url, error=str(exc)[:500]
            )
            return False
        self._manifest = None
        self._commit_sha = None
        logger.info(
            "assertion_toolbox_synced",
            repo=self._repo_url,
            ref=self._ref,
            commit=self.commit_sha(),
            templates=len(self.manifest()),
        )
        return True


def _git_sync(repo_url: str, ref: str, dest: Path) -> None:
    if (dest / ".git").is_dir():
        _run_git(["fetch", "--depth", "1", "origin", ref], cwd=dest)
        _run_git(["checkout", "--force", "FETCH_HEAD"], cwd=dest)
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    _run_git(
        ["clone", "--depth", "1", "--branch", ref, repo_url, str(dest)],
        cwd=dest.parent,
    )


def _run_git(args: list[str], *, cwd: Path) -> str:
    completed = subprocess.run(
        ["git", *args],  # 参数来自配置，非用户输入
        cwd=str(cwd),
        check=True,
        capture_output=True,
        text=True,
        timeout=120,
    )
    return completed.stdout.strip()


def _git_head_sha(root: Path) -> str:
    if not (root / ".git").is_dir():
        return UNKNOWN_REF
    try:
        return _run_git(["rev-parse", "HEAD"], cwd=root) or UNKNOWN_REF
    except (subprocess.SubprocessError, OSError):  # pragma: no cover - 环境无 git
        return UNKNOWN_REF


@lru_cache
def get_default_toolbox() -> AssertionToolbox:
    """进程内共享的默认工具箱实例（manifest 与 commit sha 只解析一次）。"""
    return AssertionToolbox()


__all__ = [
    "MANIFEST_FILENAME",
    "TEMPLATES_DIRNAME",
    "UNKNOWN_REF",
    "AssertionToolbox",
    "TemplateMatch",
    "TemplateMetadata",
    "ToolboxError",
    "extract_keywords",
    "get_default_toolbox",
    "parse_manifest",
    "score_template",
]
