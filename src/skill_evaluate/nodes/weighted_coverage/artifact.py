"""可追溯性矩阵制品：`traceability_matrix.json` / `.csv`（docs/dev/18 第 7 节）。

纯函数 + 两次写盘，不碰库、不发请求。单独成文件是为了让"制品长什么样"有一个
可以直接读的定义——它的 schema 是对外承诺（见下），埋在节点实现里会让人误以为
可以随手改。

## 为什么 JSON 与 CSV 双格式

两种消费场景并存，不用单一格式强行兼顾：

- **JSON** 供程序化 diff（未来接入的自动化漂移告警、任何下游可视化工具）。它是
  嵌套结构，能原样表达"一项能力被哪几条用例覆盖"这种一对多关系。
- **CSV** 供直接拖进表格工具人工审阅、以及 git diff 的逐行阅读。它是扁平的，
  一对多关系被摊平成分号分隔的一列。

## Schema 稳定性承诺

`traceability_matrix.json` 的字段是**对外接口**：docs/dev/18 第 7 节明确本项目
只保证它 schema 稳定、语义自洽、可被任意下游工具消费，而 Base44 热力图控制台
这类可视化层属于团队按需在运维侧对接的事，不在本仓库内实现。因此改这里的字段名
等于改一个公开契约，请当作 breaking change 对待（加字段安全，改名/删字段不安全）。
"""

from __future__ import annotations

import csv
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from skill_evaluate.state.capability import TIER_WEIGHTS, CapabilityTree

# 文件名固定，只有目录随 run_id 变。CI 归档那一步（docs/dev/24）按路径捞文件，
# 名字带上 run_id 会逼着那一步去拼字符串。
ARTIFACT_BASENAME = "traceability_matrix"

# CSV 里"一项对应多条用例"的摊平分隔符。用分号而不是逗号：逗号是 CSV 的字段
# 分隔符，摊平后再出现一次会逼着表格工具去猜引号，而 `covering_cases` 恰恰是
# 人最常复制粘贴的一列。
_MULTI_VALUE_SEPARATOR = ";"

# CSV 表头。显式写死而不是从第一行的 dict 推：推导出来的列序取决于插入顺序，
# 某次重构换了个字段位置，整份 CSV 的 diff 就全变了。
CSV_COLUMNS = (
    "kind",  # capability | negative_constraint | combinatorial_pair
    "id",
    "description",
    "tier",
    "weight",
    "covered",
    "covering_cases",
)


def build_traceability_matrix(
    tree: CapabilityTree, *, generated_at: datetime | None = None
) -> dict[str, Any]:
    """把能力树折算成可追溯性矩阵的 JSON 结构（docs/dev/18 第 7 节）。

    三段内容对应三份文档的产出：`nodes` 是模块六的二元覆盖 + 模块八的权重分级，
    `negative_constraints` 是模块八的反事实追踪，`combinatorial_coverage` 是模块七
    的组合矩阵。放在同一份制品里，是因为"这份 Skill 到底被测到了什么程度"这个
    问题只有把三者并排看才答得上来。

    `generated_at` 可注入，默认取当前 UTC 时间：测试要断言一份确定的输出，而
    "生成时间"是这份结构里唯一不确定的字段。
    """
    stamp = generated_at or datetime.now(UTC)
    return {
        "skill_id": tree.skill_id,
        "skill_version_ref": tree.skill_version_ref,
        "generated_at": stamp.isoformat(),
        "nodes": [
            {
                "id": node.capability_id,
                "description": node.description,
                "tier": node.tier.value,
                "weight": TIER_WEIGHTS[node.tier],
                "covered": node.covered,
                "covering_cases": list(node.covering_case_ids),
            }
            for node in tree.nodes
        ],
        "negative_constraints": [
            {
                "id": constraint.constraint_id,
                "description": constraint.description,
                "covered": constraint.covered,
                "covering_cases": list(constraint.covering_case_ids),
            }
            for constraint in tree.negative_constraints
        ],
        "combinatorial_coverage": {
            "covered_pairs": [list(pair) for pair in tree.combinatorial_pairs_covered],
            # 加权覆盖率与约束覆盖率一并写进制品，让这份文件**自洽**：下游工具
            # 不必为了显示一个百分比而重新实现一遍加权算法（重新实现就会漂移）。
            "weighted_coverage_ratio": tree.weighted_coverage(),
            "negative_constraint_coverage_ratio": tree.negative_constraint_coverage(),
            "tier_grading_applied": tree.tier_grading_applied(),
        },
    }


def flatten_for_csv(matrix: dict[str, Any]) -> list[dict[str, str]]:
    """把矩阵摊平成 CSV 行。

    组合对也各占一行（`kind=combinatorial_pair`，`id` 是 `a|b`）：只导出能力与
    约束的话，CSV 读者会以为组合覆盖这件事不存在，而它恰恰是最容易被忽略的那一类
    缺口。
    """
    rows: list[dict[str, str]] = []
    for node in matrix["nodes"]:
        rows.append(
            {
                "kind": "capability",
                "id": node["id"],
                "description": node["description"],
                "tier": node["tier"],
                "weight": str(node["weight"]),
                "covered": str(bool(node["covered"])).lower(),
                "covering_cases": _MULTI_VALUE_SEPARATOR.join(node["covering_cases"]),
            }
        )
    for constraint in matrix["negative_constraints"]:
        rows.append(
            {
                "kind": "negative_constraint",
                "id": constraint["id"],
                "description": constraint["description"],
                # 约束没有权重分级：它是"守没守规矩"的二元事实，与能力的重要性
                # 分档不是一回事。留空而不是填 0，避免被当成"权重为零、无关紧要"。
                "tier": "",
                "weight": "",
                "covered": str(bool(constraint["covered"])).lower(),
                "covering_cases": _MULTI_VALUE_SEPARATOR.join(constraint["covering_cases"]),
            }
        )
    for pair in matrix["combinatorial_coverage"]["covered_pairs"]:
        rows.append(
            {
                "kind": "combinatorial_pair",
                "id": "|".join(pair),
                "description": "",
                "tier": "",
                "weight": "",
                "covered": "true",  # 落库的组合对**只有**已覆盖的那些（模块七口径）
                "covering_cases": "",
            }
        )
    return rows


def write_matrix(matrix: dict[str, Any], *, artifacts_dir: str, run_id: str) -> str:
    """把矩阵写成 `<artifacts_dir>/<run_id>/traceability_matrix.{json,csv}`。

    返回 JSON 的路径（CSV 与它同名不同后缀）。按 run_id 分目录：一次 CI 里可能跑
    多个 Skill，共用一个文件名会让后跑完的把先跑完的覆盖掉。

    `ensure_ascii=False` + `utf-8`：能力描述基本都是中文，转义成 `\\uXXXX` 之后
    这份文件对人就不可读了，而人工审阅正是它存在的一半理由。CSV 写
    `utf-8-sig`（带 BOM）——Excel 打开不带 BOM 的 UTF-8 CSV 会把中文显示成乱码，
    而"拖进表格工具"正是 CSV 那一半的全部理由。
    """
    directory = Path(artifacts_dir) / run_id
    directory.mkdir(parents=True, exist_ok=True)

    json_path = directory / f"{ARTIFACT_BASENAME}.json"
    json_path.write_text(json.dumps(matrix, indent=2, ensure_ascii=False), encoding="utf-8")

    csv_path = directory / f"{ARTIFACT_BASENAME}.csv"
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(CSV_COLUMNS))
        writer.writeheader()
        writer.writerows(flatten_for_csv(matrix))

    return str(json_path)


__all__ = [
    "ARTIFACT_BASENAME",
    "CSV_COLUMNS",
    "build_traceability_matrix",
    "flatten_for_csv",
    "write_matrix",
]
