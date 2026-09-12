"""`capability_id` 的确定性生成（docs/dev/16 第 2.1 节）。

## 为什么不能用自增序号

`TestCase.target_capability_ids` 会**长期存活**在库里：模块六写入它，模块七靠它
做冗余折叠与孤儿用例检测，模块八在同一棵树上补权重分级。而能力树会被反复重新
抽取（SKILL.md 改一行、文档 18 补跑分级子任务、下一次 CI 又跑一遍）。

如果 id 是抽取顺序的函数，那么只要模型这次把"读取 Excel"排在了第 3 位而上次排
在第 5 位，历史绑定就会整体错位——报告不会报错，只会给出一份"覆盖率突然从 92%
掉到 40%"的结论，而没有人能解释为什么。

因此 id 只能是**能力语义本身的函数**：同一条能力描述，无论谁在什么时候抽取、
排在第几位，都得到同一个 id；描述被实质性改写了，就该是一个新 id（那本来就是
一项新能力，旧用例不该继续算作它的覆盖）。

## 归一化的边界

`normalize_capability_text()` 只做**书写差异**的归一（首尾空白、内部连续空白、
大小写、常见的中英标点变体），不做任何语义层面的等价判断（不做同义词替换、不做
词干还原）。理由是：语义等价一旦交给启发式，就会出现"改了个词，系统认为还是同
一项能力"与"没改语义，系统认为换了一项能力"这两类相反的错误，而两者都无法从
报告里看出来。书写归一是安全的——它只吸收"抽取两次、标点不同"这类纯噪声。
"""

from __future__ import annotations

import hashlib
import re
import unicodedata

# id 中哈希部分的长度。12 个十六进制字符 = 48 bit，在单个 Skill 的几十项能力
# 规模下碰撞概率可忽略；真的撞上了也不会静默错乱——`build_capability_nodes()`
# 会按 id 去重，两项描述不同却撞 id 的能力会被合并成一项并留下日志。
_HASH_LENGTH = 12

# id 的中缀，让 `skill-x:cap-3f9a…` 一眼能看出是能力节点（而不是用例、补丁等）。
CAPABILITY_ID_INFIX = "cap"

# 负向约束（模块八 / docs/dev/18 第 3 节）的中缀。与能力节点分开一个中缀，是因为
# 两者在 `TestCase` 上落在**不同字段**（`target_capability_ids` /
# `negative_constraint_ids`），混用同一个中缀时，一旦某处把两个列表填反，表现是
# "覆盖率与约束覆盖率同时不对"，而没有任何一处会报错；带中缀则一眼看得出。
CONSTRAINT_ID_INFIX = "neg"

# 归一化时统一收敛的标点：全角/半角、直引号/弯引号在两次抽取之间经常变化，
# 但它们从不改变能力语义。
_PUNCTUATION_FOLD = str.maketrans(
    {
        "，": ",",
        "。": ".",
        "；": ";",
        "：": ":",
        "！": "!",
        "？": "?",
        "（": "(",
        "）": ")",
        "、": ",",
        "“": '"',
        "”": '"',
        "‘": "'",
        "’": "'",
        "—": "-",
        "–": "-",
    }
)

_WHITESPACE_RE = re.compile(r"\s+")


def normalize_capability_text(description: str) -> str:
    """把能力描述归一为用于哈希的稳定形式。

    NFKC 规范化放在最前：它负责全角英文字母/数字与半角形式的统一，这类差异在
    中英混排的 SKILL.md 里非常常见，且完全不影响语义。
    """
    text = unicodedata.normalize("NFKC", description)
    text = text.translate(_PUNCTUATION_FOLD)
    text = _WHITESPACE_RE.sub(" ", text)
    return text.strip().casefold()


def build_capability_id(skill_id: str, description: str) -> str:
    """生成形如 `<skill_id>:cap-<hash12>` 的稳定能力 id。

    带 `skill_id` 前缀是为了让 id 在全库范围内自解释：模块七做孤儿用例检测时拿
    到的是一串裸 id，没有前缀就无法判断"这条用例绑的能力属于哪个 Skill"，只能
    再回查一次用例本身。前缀也顺带避免了两个 Skill 恰好写出同一句能力描述时
    共用一个 id（那会让 A 的用例被算成 B 的覆盖）。
    """
    normalized = normalize_capability_text(description)
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:_HASH_LENGTH]
    return f"{skill_id}:{CAPABILITY_ID_INFIX}-{digest}"


def build_constraint_id(skill_id: str, description: str) -> str:
    """生成形如 `<skill_id>:neg-<hash12>` 的稳定负向约束 id（docs/dev/18 第 3 节）。

    与 `build_capability_id()` **共用同一套归一化与哈希**（只换中缀），理由见
    `docs/dev/interfaces/16` 第 4.3 节：`TestCase.negative_constraint_ids` 与
    `target_capability_ids` 一样是长期存活的绑定，id 必须是"约束语义本身的函数"
    而不是抽取顺序的函数。

    两个 id 空间因此天然不会互相碰撞（中缀不同），同一句话即使既被当成能力又被
    当成约束抽出来，也会得到两个不同的 id。
    """
    normalized = normalize_capability_text(description)
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:_HASH_LENGTH]
    return f"{skill_id}:{CONSTRAINT_ID_INFIX}-{digest}"


def build_capability_tree_id(skill_id: str, skill_version_ref: str) -> str:
    """`PipelineState.capability_tree_id` 的取值（docs/dev/16 第 4 节）。

    能力树在库里的主键就是 `(skill_id, skill_version_ref)` 这对复合键
    （`uq_capability_trees_skill_version`），状态里存的这个字符串只是它的扁平
    表示，不是另一套独立 id——所以它必须能被 `parse_capability_tree_id()` 无损
    还原，不要在中间塞别的东西。
    """
    return f"{skill_id}:{skill_version_ref}"


def parse_capability_tree_id(tree_id: str) -> tuple[str, str]:
    """还原 `capability_tree_id` 为 `(skill_id, skill_version_ref)`。

    用 `rsplit(":", 1)` 而不是 `split`：`skill_id` 本身允许含冒号（约定是"仓库
    路径的 slug"，而 `org:repo/skills/foo` 这类写法很常见），而 `version_ref`
    是 git commit sha / tag，不含冒号。从右边切一刀才是唯一正确的切法。
    """
    skill_id, sep, version_ref = tree_id.rpartition(":")
    if not sep or not skill_id or not version_ref:
        raise ValueError(
            f"非法的 capability_tree_id：{tree_id!r}，期望形如 '<skill_id>:<skill_version_ref>'"
        )
    return skill_id, version_ref


__all__ = [
    "CAPABILITY_ID_INFIX",
    "CONSTRAINT_ID_INFIX",
    "build_capability_id",
    "build_capability_tree_id",
    "build_constraint_id",
    "normalize_capability_text",
    "parse_capability_tree_id",
]
