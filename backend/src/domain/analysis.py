"""按必需适配特征执行完全链接归并，保留切树及最远规格对证据。"""

from array import array
from dataclasses import dataclass
from heapq import heapify, heappop, heappush, heapreplace
from itertools import product
from math import isclose

from .spec import SPEC


_PAIR_LIMIT = 50
# 距离经 JSON/数据库往返后可能有位级浮点误差，取证时按紧容差比对。
_DISTANCE_TOL = 1e-12
_CUT = SPEC["params"][0]
_PAIR_COLUMNS = (
    ("left_id", "左侧规格"), ("right_id", "右侧规格"), ("distance", "Jaccard 距离"),
    ("left_only", "左侧独有特征"), ("right_only", "右侧独有特征"),
)


@dataclass(frozen=True)
class _Cluster:
    node: str
    members: tuple[int, ...]
    height: float = 0.0


def validate_rows(rows):
    """校验 CSV 规格行，返回可再次导入的规范化副本。"""
    if not isinstance(rows, (list, tuple)) or not rows:
        raise ValueError("内衬明细为空，请导入至少一条内衬规格。")
    normalized = []
    seen = set()
    for index, row in enumerate(rows, start=1):
        if not isinstance(row, dict):
            raise ValueError(f"第 {index} 条内衬规格格式有误，请核对样例 CSV。")
        record = {}
        for field in SPEC["fields"]:
            value = row.get(field["key"])
            if not isinstance(value, str) or not 0 < len(value.strip()) <= field["max_length"]:
                raise ValueError(f"第 {index} 条的「{field['label']}」须为 1 至 {field['max_length']} 个字符。")
            record[field["key"]] = value.strip()
        spec_id = record["spec_id"].casefold()
        if spec_id in seen:
            raise ValueError(f"第 {index} 条的规格编号「{record['spec_id']}」重复，请保留唯一规格。")
        seen.add(spec_id)
        traits = {trait.strip() for trait in record["fit_traits"].split("|")}
        if "" in traits:
            raise ValueError(f"规格「{record['spec_id']}」含空特征，请检查 | 分隔符两侧。")
        record["fit_traits"] = "|".join(sorted(traits))
        normalized.append(record)
    return normalized


def _cut_distance(params):
    if not isinstance(params, dict) or params.keys() - {_CUT["key"]}:
        raise ValueError("归并参数格式有误，请检查归并距离切线。")
    cut = params.get(_CUT["key"], _CUT["default"])
    if type(cut) not in (int, float) or not _CUT["min"] <= cut <= _CUT["max"]:
        raise ValueError("归并距离切线须为 0 至 1 的数值。")
    return cut


def prepare_records(rows):
    """规范化并按规格编号排序，拆分出名称与适配特征集合。"""
    records = sorted(validate_rows(rows), key=lambda row: row["spec_id"].casefold())
    ids = [row["spec_id"] for row in records]
    names = [row["name"] for row in records]
    traits = [set(row["fit_traits"].split("|")) for row in records]
    return records, ids, names, traits


def jaccard(left, right):
    shared = len(left & right)
    union = len(left) + len(right) - shared
    return (union - shared) / union


def pair_distance(traits, left, right):
    return jaccard(traits[left], traits[right])


def distance_matrix(traits):
    """下三角距离矩阵，matrix[right][left] 为两规格的 Jaccard 距离。"""
    matrix = []
    for right in range(len(traits)):
        distances = array("d")
        for left in range(right):
            distances.append(pair_distance(traits, left, right))
        matrix.append(distances)
    return matrix


def _incompatible_pairs(matrix, cut):
    """保留距离最高的至多 50 个超线规格对。"""
    incompatible = []
    count = 0
    for right, distances in enumerate(matrix):
        for left, height in enumerate(distances):
            if height > cut:
                count += 1
                priority = (height, -left, -right)
                if len(incompatible) < _PAIR_LIMIT:
                    heappush(incompatible, priority)
                elif priority > incompatible[0]:
                    heapreplace(incompatible, priority)
    return count, [(-left, -right, height) for height, left, right in sorted(incompatible, reverse=True)]


def _traits_text(traits):
    return " | ".join(sorted(traits)) or "—"


def _pair_row(ids, traits, left, right, distance):
    return {
        "left_id": ids[left], "right_id": ids[right], "distance": distance,
        "left_only": _traits_text(traits[left] - traits[right]),
        "right_only": _traits_text(traits[right] - traits[left]),
    }


def _hierarchy(matrix, ids, traits, cut):
    active = {i: _Cluster(f"规格 {spec_id}", (i,)) for i, spec_id in enumerate(ids)}
    nearest = [(-1.0, i, i) for i in range(len(ids) - 1)]
    heapify(nearest)
    cut_clusters = None
    merges = []
    while len(active) > 1:
        height, left, right = heappop(nearest)
        if left not in active:
            continue
        # A slot stays with its smallest leaf. Distances can only increase, so stale
        # heap entries are lower bounds. Refresh a row only when it reaches the top.
        if right not in active or right == left or matrix[right][left] != height:
            best = min(((matrix[j][left], j) for j in active if j > left), default=None)
            if best is not None:
                heappush(nearest, (best[0], left, best[1]))
            continue

        if height > cut and cut_clusters is None:
            cut_clusters = list(active.values())
        left_cluster, right_cluster = active.pop(right), active[left]
        # 矩阵中的距离可能来自上一版缓存，按紧容差找回等距见证规格对。
        witness_left, witness_right = min(
            (a, b) for a, b in product(left_cluster.members, right_cluster.members)
            if isclose(jaccard(traits[a], traits[b]), height, rel_tol=_DISTANCE_TOL, abs_tol=_DISTANCE_TOL)
        )
        step = len(merges) + 1
        parent = _Cluster(
            f"归并 {step}", tuple(sorted(left_cluster.members + right_cluster.members)), height,
        )
        merges.append({
            "step": step, "left_node": left_cluster.node, "right_node": right_cluster.node,
            "node": parent.node, "size": len(parent.members), "retained": "保留" if height <= cut else "切开",
            **_pair_row(ids, traits, witness_left, witness_right, height),
        })
        active[left] = parent
        for other in active:
            if other != left:
                target = matrix[max(left, other)]
                source = matrix[max(right, other)]
                target_index = min(left, other)
                target[target_index] = max(target[target_index], source[min(right, other)])
        heappush(nearest, (height, left, right))
    return (list(active.values()) if cut_clusters is None else cut_clusters), merges


def _cluster_row(index, cluster, ids, names, traits):
    member_traits = [traits[i] for i in cluster.members]
    shared = set.intersection(*member_traits)
    return {
        "group": f"G{index:02d}", "node": cluster.node,
        "size": len(cluster.members), "diameter": cluster.height,
        "members": "、".join(ids[i] for i in cluster.members),
        "names": "、".join(names[i] for i in cluster.members),
        "shared": _traits_text(shared), "variable": _traits_text(set.union(*member_traits) - shared),
    }


def _table(title, columns, rows):
    return {"title": title, "columns": [{"key": key, "label": label} for key, label in columns], "rows": rows}


def build_result(ids, names, traits, matrix, cut, progress=None):
    """从距离矩阵出发执行切树，整批重算与批次更正共用同一条算法链路。"""
    pair_count, incompatible = _incompatible_pairs(matrix, cut)
    clusters, merges = _hierarchy(matrix, ids, traits, cut)
    groups = [_cluster_row(index, cluster, ids, names, traits) for index, cluster in enumerate(clusters, start=1)]
    pairs = [_pair_row(ids, traits, left, right, height) for left, right, height in incompatible]
    kept = len(ids) - len(groups)
    note = f"距离切线为 {cut}，含等于切线的归并。"
    if pair_count > len(pairs):
        note += f"超线清单展示距离最高的 {len(pairs)} 对，共有 {pair_count} 对超线。"
    if progress:
        note += (
            f"本版重算 {progress['recomputed']} 个规格对，"
            f"沿用上一版 {progress['reused']} 个未受影响的规格对。"
        )
    rerun = "按候选组核对共有特征和规格间差异，记录实物打样结论。"
    if kept < len(merges):
        boundary = merges[kept]
        rerun = (
            f"第一处切开出现在第 {boundary['step']} 步，"
            f"{boundary['left_id']} 与 {boundary['right_id']} 的距离为 {boundary['distance']:.6g}。"
            "核对两侧独有特征，修订台账或切线后可重跑。"
        )
    result = {
        "state": "warning" if pair_count else "ok",
        "headline": f"{len(ids)} 条内衬规格切出 {len(groups)} 个候选组。",
        "note": note,
        "metrics": [
            {"label": "内衬规格", "value": len(ids), "detail": "纳入归并的有效台账条目"},
            {"label": "候选组", "value": len(groups), "detail": f"其中 {sum(group['size'] == 1 for group in groups)} 组仅含一条规格"},
            {"label": "保留归并", "value": kept, "detail": f"完整归并树共有 {len(merges)} 步"},
            {"label": "超线组合", "value": pair_count, "detail": "距离严格大于切线的规格对"},
        ],
        "tables": [
            _table("切树候选组", (
                ("group", "候选组"), ("node", "树节点"), ("size", "规格数"), ("diameter", "组内最大距离"),
                ("members", "规格编号"), ("names", "内衬名称"), ("shared", "共有特征"), ("variable", "差异特征"),
            ), groups),
            _table("完全链接归并轨迹", (
                ("step", "步骤"), ("left_node", "左子节点"), ("right_node", "右子节点"),
                ("node", "归并节点"), ("size", "归并后规格数"), ("retained", "切树判定"),
            ) + _PAIR_COLUMNS, merges),
            _table("超线规格组合", _PAIR_COLUMNS, pairs),
        ],
        "charts": [{
            "title": "完全链接归并高度", "kind": "line",
            "series": [{"label": f"第 {row['step']} 步", "value": row["distance"]} for row in merges],
        }],
        "sections": [{"title": "切树复核与重跑", "text": rerun}],
    }
    if progress:
        result["metrics"].append({
            "label": "本版重算规格对", "value": progress["recomputed"],
            "detail": f"至少一端较前版有改动；{progress['reused']} 个规格对沿用前版",
        })
    return result


def analyze(rows, normalized_params):
    """返回框架通用结果，切线比较包含等号且使用未舍入的距离。"""
    _records, ids, names, traits = prepare_records(rows)
    cut = _cut_distance(normalized_params)
    matrix = distance_matrix(traits)
    return build_result(ids, names, traits, matrix, cut)
