"""补丁转 PR（docs/dev/24 第 5 节；docs/dev/09 第 7 节"转成 git commit / PR 是 24 的职责"）。

## 与 docs/dev/24 正文伪码的三处出入（以代码为准，正文已同步修订）

1. **没有 `patch_repository.list_accepted_for_run(run_id)`**。`patches` 表没有 run_id 列（补丁的
   生命周期挂在 skill 上），而"本次运行最终采纳了哪份补丁"本来就写在图状态里：模块一
   `_applied_patch_id`、模块三 `_ic_applied_patch_id`、模块五 `_sec_applied_patch_id`
   （interfaces/11、13、15 第 3 节）。本模块从状态取 id，再到 `patch_application_results` 核对
   "确实应用成功"。
2. **不 `git apply patch.diff`**。补丁 diff 的作用对象不是文件：description 补丁 diff 的是
   description 字符串、正文补丁 diff 的是 `body_markdown`，且闭环是逐轮叠加的——最终补丁的
   基线是上一轮的工作副本，拿它对着仓库文件 apply 必然失败（interfaces/11 第 6 节第 3 条）。
   真正经过回归验证的是状态里的**工作副本**（`_working_skill` 等），因此本模块把工作副本相对
   原始版本的差异还原成文件内容：SKILL.md 走 `render_skill_md()`（与回归时写工作副本同一口径），
   脚本从工作副本目录读取。工作副本不可得（例如唤醒发生在另一台机器上、临时目录已不存在）时，
   才退化为"把最终补丁的 diff 重放到原始内容上"，并在 PR 正文里写明。
3. **多份补丁先确认能叠加**（interfaces/13 第 3.4 节、15 第 3.3 节）。模块三与模块五都可能改
   正文：各自相对原始正文算一份 diff，依次应用到累积结果上，冲突的那份不进 PR、写进正文的
   "未合入"清单，而不是二选一地悄悄丢掉。

## 采纳口径

- `patch_application_results.applied=True` 是前提（没真正应用成功的补丁不可能被验证过）；
- `regression_passed=True` → "回归验证通过"；
- `regression_passed` 不为 True 但状态里仍有 patch id → 只可能是闭环耗尽后**人工在 ACCEPT_PATCH
  卡片上选了 adopt**（`OptimizationLoop._suspend`）。人已明确采纳，照常进 PR，但在正文里醒目标注
  "未通过自动回归、人工采纳"。人选 abandon 时维度抛 `HumanRejectedSuspension`，状态里不会有 id。

**PR 创建后不自动合并**：评测系统的自动化边界止于"提出一个证据充分的候选修复"。
"""

from __future__ import annotations

import difflib
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from skill_evaluate.agents.optimizer.patch_applier import (
    SKILL_MD_TARGET,
    WORKING_COPY_MARKER,
    apply_unified_diff,
    render_skill_md,
)
from skill_evaluate.config import get_settings
from skill_evaluate.errors import DeliveryError, PatchApplyError
from skill_evaluate.graph.git_ops import GitOps, SubprocessGitOps
from skill_evaluate.logging import get_logger
from skill_evaluate.nodes.instruction_control import state as ic_state
from skill_evaluate.nodes.security import state as sec_state
from skill_evaluate.nodes.trigger_accuracy import state as trigger_state
from skill_evaluate.persistence.repository import PatchRepository, RunRepository, SkillRepository
from skill_evaluate.state.enums import PatchType
from skill_evaluate.state.patch import Patch, PatchApplicationResult
from skill_evaluate.state.skill import SkillDefinition

logger = get_logger(component="patch_pr")

# PR 正文里单条回归详情的截断长度：`detail` 可能拼了几十条用例的判定摘要。
_DETAIL_EXCERPT_CHARS = 800
# 分支名里 skill_id 的非法字符替换：git ref 不允许空格、`~^:?*[\` 等。
_REF_UNSAFE = set(" ~^:?*[\\")


@dataclass(frozen=True, slots=True)
class PatchSource:
    """一个会产出补丁的维度在图状态里的两个键。"""

    dimension: str
    patch_id_key: str
    working_skill_key: str


# 合入顺序即冲突时的优先级：安全修复 > 触发准确度 > 指令控制力。安全补丁通过了"安全重测 +
# 强制功能回归"两道闸（interfaces/15 第 3.3 节），与其他补丁冲突时保留它更稳妥；模块一只改
# description，与另外两者天然不冲突，排第二即可。
PATCH_SOURCES: tuple[PatchSource, ...] = (
    PatchSource(sec_state.DIMENSION, sec_state.KEY_APPLIED_PATCH_ID, sec_state.KEY_WORKING_SKILL),
    PatchSource(
        trigger_state.DIMENSION, trigger_state.KEY_APPLIED_PATCH_ID, trigger_state.KEY_WORKING_SKILL
    ),
    PatchSource(ic_state.DIMENSION, ic_state.KEY_APPLIED_PATCH_ID, ic_state.KEY_WORKING_SKILL),
)


class AcceptedPatch(BaseModel):
    """一份确定要进 PR 的补丁及其证据。"""

    dimension: str
    patch: Patch
    application: PatchApplicationResult
    # 回归验证通过的工作副本（checkpoint 反序列化后可能是 dict，入口处统一校验成模型）。
    working_skill: SkillDefinition | None = None

    @property
    def regression_passed(self) -> bool:
        return self.application.regression_passed is True


class ComposedChanges(BaseModel):
    """把多份补丁还原为文件内容后的结果（相对 Skill 根目录的路径 → 新内容）。"""

    files: dict[str, str] = Field(default_factory=dict)
    included_patch_ids: list[str] = Field(default_factory=list)
    conflicts: list[str] = Field(default_factory=list)  # 未能合入的补丁及原因（进 PR 正文）
    notes: list[str] = Field(default_factory=list)  # 合入方式的补充说明（如退化为 diff 重放）


PullRequestStatus = Literal["created", "reused", "skipped", "failed"]


class PullRequestOutcome(BaseModel):
    """`finalize.patch_pr` 的结果，写进图状态与报告尾部。"""

    status: PullRequestStatus
    url: str | None = None
    branch: str | None = None
    reason: str = ""
    patches: list[dict[str, Any]] = Field(default_factory=list)
    conflicts: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


# --------------------------------------------------------------------------- #
# 1. 从图状态收集已采纳的补丁
# --------------------------------------------------------------------------- #


async def collect_accepted_patches(
    state: Mapping[str, Any], patch_repository: PatchRepository
) -> tuple[list[AcceptedPatch], list[str]]:
    """返回 (已采纳补丁, 被排除的补丁说明)。排除的原因同样进 PR 结果，便于排查"为什么没提 PR"。"""
    accepted: list[AcceptedPatch] = []
    excluded: list[str] = []
    for source in PATCH_SOURCES:
        patch_id = state.get(source.patch_id_key)
        if not patch_id:
            continue
        patch = await patch_repository.get(str(patch_id))
        application = await patch_repository.get_application_result(str(patch_id))
        if patch is None or application is None or not application.applied:
            excluded.append(
                f"{source.dimension}：补丁 {patch_id} 没有成功应用的记录，未纳入 PR"
            )
            continue
        accepted.append(
            AcceptedPatch(
                dimension=source.dimension,
                patch=patch,
                application=application,
                working_skill=_as_skill(state.get(source.working_skill_key)),
            )
        )
    return accepted, excluded


def _as_skill(value: Any) -> SkillDefinition | None:
    if value is None:
        return None
    if isinstance(value, SkillDefinition):
        return value
    if isinstance(value, Mapping):
        return SkillDefinition.model_validate(dict(value))
    return None


# --------------------------------------------------------------------------- #
# 2. 把补丁还原为文件内容（纯逻辑，文件读取由调用方注入）
# --------------------------------------------------------------------------- #

FileReader = Callable[[str], str | None]


def compose_changes(
    original: SkillDefinition, accepted: list[AcceptedPatch], read_file: FileReader
) -> ComposedChanges:
    """把已采纳补丁合成为一组文件改动。

    `read_file(rel_path)` 读取**基线提交**里 Skill 根目录下的文件（PR worktree 里读），不存在返回
    None。所有差异都相对 `original`（数据库里被评测的那个版本）计算，逐份叠加。
    """
    result = ComposedChanges()
    description = original.description
    body = original.body_markdown
    description_owner: str | None = None
    code_files: dict[str, str] = {}

    for item in accepted:
        contributed = False
        working = item.working_skill
        if working is None:
            # 退化路径（理论上只在 checkpoint 被人为裁剪时出现）：只能表达"最终补丁"这一份
            # diff 的效果，重放结果按补丁类型写回累积状态。
            result.notes.append(
                f"{item.dimension}：状态里缺少工作副本，按最终补丁 {item.patch.patch_id} 的 diff 重放"
            )
            if item.patch.patch_type is PatchType.CODE_PATCH:
                contributed = _replay_code_diff(item, read_file, result, code_files)
            else:
                description, body, contributed = _apply_replayed_text(
                    item, description, body, result
                )
        else:
            # ---- description：只有模块一会改；两个维度改成不同内容时保留先合入的 ----
            if working.description != original.description:
                if description_owner is not None and description != working.description:
                    result.conflicts.append(
                        f"{item.dimension}（补丁 {item.patch.patch_id}）：description 与 "
                        f"{description_owner} 的改动不一致，未合入"
                    )
                else:
                    description = working.description
                    description_owner = item.dimension
                    contributed = True
            # ---- 正文：相对原始正文算 diff，叠加到累积结果上 ----
            if working.body_markdown != original.body_markdown:
                diff = make_text_diff(original.body_markdown, working.body_markdown)
                try:
                    body = apply_unified_diff(body, diff)
                    contributed = True
                except PatchApplyError as exc:
                    result.conflicts.append(
                        f"{item.dimension}（补丁 {item.patch.patch_id}）：正文改动与先合入的补丁冲突，"
                        f"未合入（{str(exc).splitlines()[0]}）"
                    )
            # ---- 脚本：从工作副本目录读回 ----
            if item.patch.patch_type is PatchType.CODE_PATCH:
                contributed |= _collect_code_changes(item, working, read_file, result, code_files)

        if contributed:
            result.included_patch_ids.append(item.patch.patch_id)

    if description != original.description or body != original.body_markdown:
        raw = read_file(SKILL_MD_TARGET)
        if raw is None:
            result.conflicts.append("基线提交里找不到 SKILL.md，SKILL.md 相关改动未合入")
        else:
            rendered = render_skill_md(
                raw,
                description=description if description != original.description else None,
                body=body if body != original.body_markdown else None,
            )
            if rendered != raw:
                result.files[SKILL_MD_TARGET] = rendered
    result.files.update(code_files)
    return result


def make_text_diff(before: str, after: str) -> str:
    """生成 `apply_unified_diff()` 能消费的 unified diff（3 行上下文）。"""
    return "\n".join(
        difflib.unified_diff(
            before.splitlines(), after.splitlines(), fromfile="a", tofile="b", lineterm=""
        )
    )


def _collect_code_changes(
    item: AcceptedPatch,
    working: SkillDefinition,
    read_file: FileReader,
    result: ComposedChanges,
    code_files: dict[str, str],
) -> bool:
    """代码补丁：优先读工作副本里被改过的文件，工作副本不在了就重放最终补丁 diff。"""
    working_root = Path(working.root_path)
    if not (working_root / WORKING_COPY_MARKER).is_file():
        result.notes.append(
            f"{item.dimension}：代码补丁的工作副本已不存在（可能在另一台机器上被唤醒执行），"
            f"按最终补丁 {item.patch.patch_id} 的 diff 重放到基线文件上——多轮叠加的中间改动不会体现，"
            "请重点审查该文件"
        )
        return _replay_code_diff(item, read_file, result, code_files)

    contributed = False
    for path in sorted(working_root.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(working_root).as_posix()
        # SKILL.md 由上面的 description/正文逻辑统一渲染；标记文件是评测系统自己的。
        if rel in (SKILL_MD_TARGET, WORKING_COPY_MARKER):
            continue
        try:
            new_content = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue  # 二进制文件不是 Optimizer 能改的对象
        if new_content == read_file(rel):
            continue
        if not _record_file(item, rel, new_content, result, code_files):
            continue
        contributed = True
    return contributed


def _replay_code_diff(
    item: AcceptedPatch, read_file: FileReader, result: ComposedChanges, code_files: dict[str, str]
) -> bool:
    rel = item.patch.target_path
    base = code_files.get(rel, read_file(rel))
    if base is None:
        result.conflicts.append(f"{item.dimension}：基线里找不到补丁目标文件 {rel}，未合入")
        return False
    try:
        new_content = apply_unified_diff(base, item.patch.diff)
    except PatchApplyError as exc:
        result.conflicts.append(
            f"{item.dimension}（补丁 {item.patch.patch_id}）：diff 无法重放到 {rel}，未合入"
            f"（{str(exc).splitlines()[0]}）"
        )
        return False
    return _record_file(item, rel, new_content, result, code_files)


def _record_file(
    item: AcceptedPatch,
    rel: str,
    content: str,
    result: ComposedChanges,
    code_files: dict[str, str],
) -> bool:
    existing = code_files.get(rel)
    if existing is not None and existing != content:
        result.conflicts.append(
            f"{item.dimension}（补丁 {item.patch.patch_id}）：{rel} 已被先合入的补丁改成不同内容，未合入"
        )
        return False
    code_files[rel] = content
    return True


def _apply_replayed_text(
    item: AcceptedPatch, description: str, body: str, result: ComposedChanges
) -> tuple[str, str, bool]:
    """把最终补丁的 diff 重放到累积的 description / 正文上，返回 (description, body, 是否合入)。"""
    try:
        if item.patch.patch_type is PatchType.DESCRIPTION_PATCH:
            return apply_unified_diff(description, item.patch.diff).strip(), body, True
        return description, apply_unified_diff(body, item.patch.diff), True
    except PatchApplyError as exc:
        result.conflicts.append(
            f"{item.dimension}（补丁 {item.patch.patch_id}）：diff 无法重放，未合入"
            f"（{str(exc).splitlines()[0]}）"
        )
        return description, body, False


# --------------------------------------------------------------------------- #
# 3. PR 元信息
# --------------------------------------------------------------------------- #


def branch_name_for(skill_id: str, run_id: str, *, prefix: str | None = None) -> str:
    """`<prefix>/<skill_id>/<run_id 前 8 位>`（docs/dev/24 第 5 节）。skill_id 里的非法 ref 字符替换为 `-`。"""
    safe_skill = "".join("-" if ch in _REF_UNSAFE else ch for ch in skill_id).strip("/.") or "skill"
    head = prefix if prefix is not None else get_settings().pipeline.patch_pr_branch_prefix
    return f"{head.rstrip('/')}/{safe_skill}/{run_id[:8]}"


def render_pr_body(
    *,
    run_id: str,
    skill: SkillDefinition,
    accepted: list[AcceptedPatch],
    composed: ComposedChanges,
    report_link: str,
) -> str:
    """PR 正文：每个补丁的理由、回归验证摘要、benchmark 报告链接（docs/dev/24 第 5 节）。"""
    lines = [
        "## skill-evaluate 自动修复候选",
        "",
        f"- Skill：`{skill.skill_id}` @ `{skill.version_ref}`",
        f"- 评测运行：`{run_id}`",
        f"- Benchmark 报告：{report_link}",
        "",
        (
            "> 本 PR 由评测流水线自动创建，**不会自动合并**。每一处改动都经过了对应维度的自动回归"
            "（或经人工审批采纳，见下方标注），合并与否请走常规 Code Review。"
        ),
        "",
    ]
    included = set(composed.included_patch_ids)
    for item in accepted:
        verdict = (
            "✅ 回归验证通过"
            if item.regression_passed
            else "⚠️ **未通过自动回归，经人工审批采纳（ACCEPT_PATCH → adopt）**"
        )
        state_note = "" if item.patch.patch_id in included else "（**未合入本 PR**，见下方冲突说明）"
        lines += [
            f"### {item.dimension}：`{item.patch.patch_type.value}` → `{item.patch.target_path}`{state_note}",
            "",
            f"- 补丁 id：`{item.patch.patch_id}`",
            f"- 回归结论：{verdict}",
            "",
            "**修复理由**",
            "",
            _quote(item.patch.rationale),
            "",
        ]
        if item.application.detail:
            lines += ["**回归详情（节选）**", "", _quote(item.application.detail[:_DETAIL_EXCERPT_CHARS]), ""]
    if composed.conflicts:
        lines += ["### 未合入的改动", ""] + [f"- {c}" for c in composed.conflicts] + [""]
    if composed.notes:
        lines += ["### 合入说明", ""] + [f"- {n}" for n in composed.notes] + [""]
    return "\n".join(lines)


def _quote(text: str) -> str:
    return "\n".join(f"> {line}" if line else ">" for line in text.strip().splitlines()) or "> （空）"


def _patch_summaries(accepted: list[AcceptedPatch], included: list[str]) -> list[dict[str, Any]]:
    return [
        {
            "dimension": item.dimension,
            "patch_id": item.patch.patch_id,
            "patch_type": item.patch.patch_type.value,
            "target_path": item.patch.target_path,
            "regression_passed": item.regression_passed,
            "included": item.patch.patch_id in included,
        }
        for item in accepted
    ]


# --------------------------------------------------------------------------- #
# 4. 节点实现
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class PatchToPullRequest:
    """`finalize.patch_pr` 的实现体。依赖全部可注入，单测不碰 git / 数据库。"""

    git_ops: GitOps | None = None
    patch_repository: PatchRepository = field(default_factory=PatchRepository)
    skill_repository: SkillRepository = field(default_factory=SkillRepository)
    run_repository: RunRepository = field(default_factory=RunRepository)
    # None = 读 `PipelineSettings.patch_pr_enabled`。
    enabled: bool | None = None

    def git(self) -> GitOps:
        if self.git_ops is None:
            self.git_ops = SubprocessGitOps()
        return self.git_ops

    async def run(self, state: Mapping[str, Any], *, report_link: str) -> PullRequestOutcome:
        """执行补丁转 PR。**从不抛异常**：交付失败记进结果，不让已出的报告变成失败的流水线。"""
        run_id = str(state["run_id"])
        try:
            accepted, excluded = await collect_accepted_patches(state, self.patch_repository)
        except Exception as exc:  # noqa: BLE001 - 数据库故障同样只影响交付，不影响评测结论
            logger.error("patch_pr_collect_failed", run_id=run_id, error=str(exc)[:500])
            return PullRequestOutcome(status="failed", reason=f"读取补丁记录失败：{exc}"[:500])

        if not accepted:
            return PullRequestOutcome(
                status="skipped", reason="本次运行没有已采纳的补丁", notes=excluded
            )
        summaries = _patch_summaries(accepted, [a.patch.patch_id for a in accepted])
        enabled = get_settings().pipeline.patch_pr_enabled if self.enabled is None else self.enabled
        if not enabled:
            # 仍把候选补丁列出来：本地跑评测的人应该知道"本来可以提一个 PR"。
            return PullRequestOutcome(
                status="skipped",
                reason="SKILLEVAL_PIPELINE_PATCH_PR_ENABLED=false，未创建 PR",
                patches=summaries,
                notes=excluded,
            )

        try:
            return await self._deliver(state, accepted, excluded, report_link=report_link)
        except (DeliveryError, PatchApplyError, OSError) as exc:
            logger.error("patch_pr_failed", run_id=run_id, error=str(exc)[:500])
            return PullRequestOutcome(
                status="failed", reason=str(exc)[:1000], patches=summaries, notes=excluded
            )

    async def _deliver(
        self,
        state: Mapping[str, Any],
        accepted: list[AcceptedPatch],
        excluded: list[str],
        *,
        report_link: str,
    ) -> PullRequestOutcome:
        run_id = str(state["run_id"])
        skill_id = str(state["skill_id"])
        version_ref = str(state["skill_version_ref"])
        summaries = _patch_summaries(accepted, [a.patch.patch_id for a in accepted])

        skill = await self.skill_repository.get(skill_id, version_ref)
        if skill is None:
            raise DeliveryError(f"skills 表里找不到 {skill_id}@{version_ref}，无法定位要修改的文件")

        git = self.git()
        repo = await git.repo_root(skill.root_path)
        if repo is None:
            return PullRequestOutcome(
                status="skipped", reason=f"{skill.root_path} 不在 git 仓库里", patches=summaries
            )
        # `+dirty:` / `sha256:` 版本号说明被评测的是未提交的工作区，没有可以作为分支起点的提交；
        # 基于当前 HEAD 提 PR 会把"评测时看到的内容"和"PR 基线"对不上。
        if not await git.is_commit(repo, version_ref):
            return PullRequestOutcome(
                status="skipped",
                reason=f"skill_version_ref={version_ref!r} 不是仓库里的提交（评测的是未提交的工作区），不自动提 PR",
                patches=summaries,
            )
        skill_rel = Path(skill.root_path).resolve().relative_to(Path(repo).resolve())

        branch = branch_name_for(skill_id, run_id)
        worktree = await git.create_worktree(repo, branch, version_ref)
        try:
            skill_dir = Path(worktree) / skill_rel

            def read_file(rel: str) -> str | None:
                path = skill_dir / rel
                try:
                    return path.read_text(encoding="utf-8") if path.is_file() else None
                except UnicodeDecodeError:
                    return None

            composed = compose_changes(skill, accepted, read_file)
            summaries = _patch_summaries(accepted, composed.included_patch_ids)
            if not composed.files:
                return PullRequestOutcome(
                    status="skipped",
                    reason="补丁合成后没有任何文件改动",
                    branch=branch,
                    patches=summaries,
                    conflicts=composed.conflicts,
                    notes=[*excluded, *composed.notes],
                )
            for rel, content in composed.files.items():
                target = (skill_dir / rel).resolve()
                # 与 patch_applier 同一条防线：路径来自模型输出，越出 Skill 目录一律拒绝。
                if not target.is_relative_to(skill_dir.resolve()):
                    raise DeliveryError(f"补丁目标路径越出 Skill 目录：{rel!r}")
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content, encoding="utf-8")

            message = f"[skill-evaluate] 自动修复: {skill_id}\n\nrun_id: {run_id}\npatches: " + ", ".join(
                composed.included_patch_ids
            )
            if not await git.commit_all(worktree, message):
                return PullRequestOutcome(
                    status="skipped", reason="写入后与基线无差异", branch=branch, patches=summaries
                )
            await git.push(worktree, branch)

            existing = await git.find_open_pull_request(repo, branch)
            status: PullRequestStatus
            if existing:
                url, status = existing, "reused"
            else:
                body = render_pr_body(
                    run_id=run_id, skill=skill, accepted=accepted, composed=composed, report_link=report_link
                )
                base = get_settings().pipeline.patch_pr_base_branch or None
                url = await git.create_pull_request(
                    worktree,
                    branch=branch,
                    title=f"[skill-evaluate] 自动修复: {skill_id}",
                    body=body,
                    base=base,
                )
                status = "created"
        finally:
            await git.remove_worktree(repo, worktree)

        await self.run_repository.record_pr_url(run_id, url)
        logger.info("patch_pr_delivered", run_id=run_id, url=url, status=status, branch=branch)
        return PullRequestOutcome(
            status=status,
            url=url,
            branch=branch,
            patches=summaries,
            conflicts=composed.conflicts,
            notes=[*excluded, *composed.notes],
        )


__all__ = [
    "PATCH_SOURCES",
    "AcceptedPatch",
    "ComposedChanges",
    "PatchSource",
    "PatchToPullRequest",
    "PullRequestOutcome",
    "branch_name_for",
    "collect_accepted_patches",
    "compose_changes",
    "make_text_diff",
    "render_pr_body",
]
