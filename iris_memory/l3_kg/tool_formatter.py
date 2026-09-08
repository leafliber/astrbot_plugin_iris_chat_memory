"""知识图谱工具结果格式化；仅使用本次检索已返回的节点。"""

import math

from .models import RELATION_TYPE_DESCRIPTIONS


def _text(value) -> str:
    return " ".join(str(value or "").split())


def _clip(value, limit: int) -> str:
    text = _text(value)
    return text if len(text) <= limit else text[:limit] + "..."


def _node_text(node: dict, content_limit: int) -> str:
    name = _clip(node.get("name"), 80)
    content = _clip(node.get("content"), content_limit)
    label = _clip(node.get("label"), 30)
    if not name:
        name = "未命名实体"
        if node.get("id"):
            name += f"（ID: {_clip(node['id'], 80)}）"
    result = f"[{label}] {name}" if label else name
    if content and content != name:
        result += f"：{content}"
    return result


def _endpoint(edge: dict, side: str, nodes_by_id: dict) -> str:
    legacy_key = "_src" if side == "source" else "_dst"
    node_id = edge.get(side) or edge.get(f"{side}_id") or edge.get(legacy_key)
    if node_id in nodes_by_id:
        # 使用完整的返回节点集，不能只使用前十个展示实体。
        return _node_text(nodes_by_id[node_id], 100)
    name = _clip(edge.get(f"{side}_name"), 80)
    if name:
        return name
    if node_id:
        return f"未知实体（ID: {_clip(node_id, 80)}；本次未返回详情）"
    return "未知实体（缺少节点标识）"


def _count(title: str, total: int, shown: int, unit: str) -> str:
    if shown < total:
        return f"**{title}**（展示 {shown} / 共 {total} {unit}）："
    return f"**{title}**（{total} {unit}）："


def format_graph_results(
    matched_nodes: list[dict],
    expanded_nodes: list[dict],
    expanded_edges: list[dict],
    query: str,
) -> str:
    """将已检索到的实体和边转换为可读文本，不额外读取或修改图谱。"""
    nodes_by_id = {
        node["id"]: node
        for node in [*expanded_nodes, *matched_nodes]
        if node.get("id")
    }
    lines = [f"## 知识图谱搜索结果 - 「{query}」", ""]
    lines.append(_count("匹配实体", len(matched_nodes), len(matched_nodes), "个"))
    for index, node in enumerate(matched_nodes, 1):
        try:
            confidence = float(node.get("confidence") or 0)
        except (TypeError, ValueError):
            confidence = 0.0
        if not math.isfinite(confidence):
            confidence = 0.0
        lines.append(
            f"{index}. {_node_text(node, 150)}（置信度: {confidence:.2f}）"
        )

    matched_ids = {node.get("id") for node in matched_nodes}
    additional_nodes = [
        node for node in expanded_nodes if node.get("id") not in matched_ids
    ]
    if additional_nodes:
        shown_nodes = additional_nodes[:10]
        lines.extend([
            "",
            _count("关联实体", len(additional_nodes), len(shown_nodes), "个"),
        ])
        lines.extend(
            f"{index}. {_node_text(node, 100)}"
            for index, node in enumerate(shown_nodes, 1)
        )

    if expanded_edges:
        shown_edges = expanded_edges[:15]
        lines.extend([
            "",
            _count("关联关系", len(expanded_edges), len(shown_edges), "条"),
        ])
        for index, edge in enumerate(shown_edges, 1):
            source = _endpoint(edge, "source", nodes_by_id)
            target = _endpoint(edge, "target", nodes_by_id)
            relation = _text(edge.get("relation_type")) or "RELATED_TO"
            description = RELATION_TYPE_DESCRIPTIONS.get(relation)
            relation_label = (
                f"{description.split('——', 1)[0]} ({relation})"
                if description else relation
            )
            lines.append(f"{index}. {source} —[{relation_label}]→ {target}")

    return "\n".join(lines)
