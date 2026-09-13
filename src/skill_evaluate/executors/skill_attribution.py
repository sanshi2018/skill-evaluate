"""多技能并发执行下"到底加载了哪个 Skill"的归因（docs/dev/20 落地时新增）。

## 为什么需要这一层

单技能执行时 `ExecutionTrace.loaded_skill_md` 语义清楚：沙箱里只有一份 SKILL.md。
挂载了 `background_skills` 之后，沙箱里同时存在 4~6 份 SKILL.md，两个问题随之出现：

1. **兜底判定会误报**。`map_hermes_payload_to_trace()` 在运行时没有显式上报
   `skill_md_loaded` 时，扫描 `read_file` 路径里是否含 `SKILL.md`——背景技能的
   SKILL.md 同样命中，于是"只加载了干扰技能"会被读成"加载了目标技能"，触发劫持
   恰好被这条兜底完美掩盖。
2. **背景过触发无从观测**。`loaded_skill_md` 是一个布尔，回答不了"干扰包里是哪一个
   被意外激活了"。

`ExecutionTrace` 上加一个 `loaded_skill_ids` 字段需要改库表与两个后端的映射，而
轨迹里本来就有答案——Agent 读了哪个路径下的 SKILL.md。因此这里基于**挂载目录约定**
（`docs/dev/interfaces/03_hermes_sandbox_client.md` "多技能挂载" 一节）做路径归因：
每个 Skill 挂载在以其 `skill_id` 命名的独立目录下（或保留其 `root_path` 的目录名）。

放在 `executors/` 而不是 `nodes/multi_skill/`：它是"Trace 证据怎么读"的执行层语义，
与 `executors/comparison.py` 同一层；将来共识门控或其他维度挂背景技能时也从这里取。

## 与显式上报冲突时怎么办：记为"证据矛盾"，不猜

`loaded_skill_md=True`，但轨迹里 SKILL.md 读取**全部**能归到背景技能、没有一次归到
目标——这有两种解释：运行时预加载了目标（轨迹里看不到读取动作），或者兜底判定
被背景技能的读取误导。二者从 Trace 上无法区分，本模块把它标记为
`contradictory=True`，调用方按"证据不足"处理并交人工，而不是替它选一个解释。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import PurePosixPath

from skill_evaluate.state.skill import SkillDefinition
from skill_evaluate.state.trace import ExecutionTrace

# 视为"读取文件"的动作类型。与 `hermes_backend` 兜底判定口径一致（`read_file`），
# 另收 `cat`/`view` 这类常见等价工具名：干扰包场景下 Agent 读文件的方式更杂。
READ_ACTION_TYPES: frozenset[str] = frozenset({"read_file", "view", "cat", "open_file"})
_PATH_KEYS: tuple[str, ...] = ("path", "file_path", "filename", "file")
_SKILL_MD = "SKILL.md"


def skill_mount_markers(skill: SkillDefinition) -> frozenset[str]:
    """一个 Skill 在沙箱里可被识别的目录名：`skill_id` 与 `root_path` 的末级目录名。

    `root_path` 为 `.` / 空时不产生标记——那种目录名对任何路径都没有区分度。
    """
    markers = {skill.skill_id}
    tail = PurePosixPath(skill.root_path.replace("\\", "/")).name
    if tail and tail not in {".", ".."}:
        markers.add(tail)
    return frozenset(markers)


def _skill_md_paths(trace: ExecutionTrace) -> list[str]:
    """轨迹里所有"读取了某份 SKILL.md"的路径（按出现顺序）。"""
    paths: list[str] = []
    for action in trace.actions:
        if action.action_type not in READ_ACTION_TYPES:
            continue
        for key in _PATH_KEYS:
            value = action.action_input.get(key)
            if isinstance(value, str) and PurePosixPath(value.replace("\\", "/")).name == _SKILL_MD:
                paths.append(value.replace("\\", "/"))
                break
    return paths


def _owner_of(path: str, skills: Sequence[SkillDefinition]) -> str | None:
    """SKILL.md 路径的直接父目录名命中哪个 Skill 的挂载标记；零个或多个命中都返回 None。

    只看**直接父目录**而不是路径里任意一段：`skills/csv-cleaner/examples/other/SKILL.md`
    这类嵌套示例不应被归到 csv-cleaner。多个 Skill 共用同一个目录名（例如两个 Skill 的
    root_path 都叫 `skill`）时归因不出来，宁可不归因也不瞎归。
    """
    parent = PurePosixPath(path).parent.name
    owners = {skill.skill_id for skill in skills if parent in skill_mount_markers(skill)}
    return next(iter(owners)) if len(owners) == 1 else None


@dataclass(frozen=True, slots=True)
class SkillLoadAttribution:
    """一条多技能并发 Trace 的加载归因结论。"""

    target_loaded: bool  # 目标 Skill 是否被加载（已结合显式上报与路径证据）
    background_loaded_ids: tuple[str, ...] = ()  # 被加载的背景技能（按 skill_id 排序）
    unattributed_reads: int = 0  # 读了 SKILL.md 但归不到任何一个已挂载 Skill 的次数
    # 显式/兜底的 loaded_skill_md 与路径证据互相矛盾（见模块头），调用方应记为证据不足。
    contradictory: bool = False
    notes: tuple[str, ...] = field(default_factory=tuple)


def attribute_skill_loads(
    trace: ExecutionTrace,
    *,
    target: SkillDefinition,
    background: Sequence[SkillDefinition],
) -> SkillLoadAttribution:
    """按挂载目录约定，把轨迹里的 SKILL.md 读取归到具体的 Skill。

    判定顺序：

    1. 有一次读取归到目标 → 目标已加载（路径证据是最具体的证据）；
    2. 没有归到目标、但 `loaded_skill_md=True`：
       - 所有 SKILL.md 读取都归到了背景技能 → `contradictory`（见模块头）；
       - 否则（没有任何读取动作 = 运行时预加载 / 有归不了的读取）→ 采信 `loaded_skill_md`；
    3. 其余 → 目标未加载。

    背景技能列表与目标重名（调用方没去重）时，目标优先，避免同一次读取被算两遍。
    """
    others = [skill for skill in background if skill.skill_id != target.skill_id]
    universe = [target, *others]
    target_hit = False
    background_hits: set[str] = set()
    unattributed = 0
    for path in _skill_md_paths(trace):
        owner = _owner_of(path, universe)
        if owner is None:
            unattributed += 1
        elif owner == target.skill_id:
            target_hit = True
        else:
            background_hits.add(owner)

    contradictory = False
    notes: list[str] = []
    if target_hit:
        target_loaded = True
    elif trace.loaded_skill_md:
        if background_hits and unattributed == 0:
            contradictory = True
            target_loaded = False
            notes.append(
                "loaded_skill_md=True 但轨迹中的 SKILL.md 读取全部属于背景技能，无法确认目标是否被加载"
            )
        else:
            target_loaded = True
    else:
        target_loaded = False

    return SkillLoadAttribution(
        target_loaded=target_loaded,
        background_loaded_ids=tuple(sorted(background_hits)),
        unattributed_reads=unattributed,
        contradictory=contradictory,
        notes=tuple(notes),
    )


__all__ = [
    "READ_ACTION_TYPES",
    "SkillLoadAttribution",
    "attribute_skill_loads",
    "skill_mount_markers",
]
