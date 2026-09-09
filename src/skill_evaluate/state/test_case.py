"""测试用例与测试集（docs/dev/02 第 5 节）。"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from skill_evaluate.state.enums import AttackSubtype, DatasetSplit, TestCaseCategory


class TestCase(BaseModel):
    case_id: str
    skill_id: str
    category: TestCaseCategory
    split: DatasetSplit
    prompt: str
    expected_output: str | None = None  # 供 Validator Agent 做断言规划
    target_capability_ids: list[str] = Field(default_factory=list)  # 模块六：与能力树的绑定
    negative_constraint_ids: list[str] = Field(default_factory=list)  # 模块八：反事实/避坑用例绑定
    seed_anchor_id: str | None = None  # 模块十一子节点二：真实种子锚点溯源
    # 模块三（docs/dev/13 第 3.1 节）新增：渐进式披露**动态**探查用例专用。
    # 取值是 `SkillDefinition.reference_files[i].path`（如 "references/errors.md"），
    # 语义是"这条用例的场景精确匹配该参考文件声明的触发条件，因此执行时应当
    # 观测到 Agent 读取了它"。为什么落在用例上而不是运行时再推断：探查判定
    # （读了/没读）必须与出题时的意图一一对应，事后靠关键词猜"这条题想探哪个
    # 文件"会把生成阶段的确定信息重新变成一次不可靠的推断。
    # 其余类别的用例恒为 None。
    probe_target_reference: str | None = None
    # 模块五（docs/dev/15 第 2 节）新增：这条 ADVERSARIAL 用例具体在打哪个攻击面。
    # 六类探测节点按它挑自己该跑的用例子集，判定方式也随之不同（注入看语义、
    # 穿越看路径、DoS 看超时）。为什么落在用例上而不是执行时再推断：出题时
    # Attacker Agent 是知道自己在构造哪类攻击的，事后靠关键词去猜等于把一个
    # 确定的事实重新变成一次不可靠的推断（与 probe_target_reference 同一条理由）。
    # 其余类别的用例恒为 None。
    attack_subtype: AttackSubtype | None = None
    generator_run_id: str  # 由哪一次 Generator 生成，供缓存复用判定
    created_at: datetime


class TestSuiteVersion(BaseModel):
    """一次 Generator 产出的完整快照，落库为独立版本，支撑"默认复用/强制重生"语义。"""

    suite_version_id: str
    skill_id: str
    skill_version_ref: str  # 绑定生成时的 SKILL.md 版本，版本不匹配触发 staleness 检查
    generation_mode: str  # GenerationMode 枚举值
    case_ids: list[str]
    created_at: datetime
    is_active: bool = True  # 同一 skill_id 下只有一个 active 版本，供流水线默认拉取
