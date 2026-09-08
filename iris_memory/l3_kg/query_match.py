"""图谱查询：稳定身份优先，只检索名称、正文与受控别名字段。"""
import heapq
import json
import math
import re
import unicodedata

# 仅将独立数字识别为 ID；昵称、邮件或连字符名称中的数字属于名称。
_USER_ID = re.compile(r"(?<![\w.@+-])[0-9]{5,20}(?![\w.@+-])")
_TAGGED_ID = re.compile(r"^\[用户:(\d{5,20})\](?:\s|$)")


def normalize(value):
    return " ".join(unicodedata.normalize("NFKC", str(value or "")).casefold().split())


def query_parts(query):
    query = str(query or "").strip()
    ids = list(dict.fromkeys(_USER_ID.findall(query)))[:8]
    terms = list(dict.fromkeys(re.findall(r"[\w\u4e00-\u9fff]+", query, flags=re.UNICODE)))[:8]
    terms = [t for t in terms if len(t) >= 2 and t not in ids]
    return query, ids, terms


def properties(node):
    raw = node.get("properties") or {}
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (ValueError, TypeError):
            raw = {}
    return raw if isinstance(raw, dict) else {}


def identity(node):
    props = properties(node)
    name = str(node.get("name") or "").strip()
    uid = str(props.get("user_id") or "").strip()
    if not uid and name.isdigit():
        uid = name
    if not uid:
        match = _TAGGED_ID.match(name)
        uid = match.group(1) if match else ""
    aliases = props.get("aliases") or []
    if isinstance(aliases, str):
        aliases = re.split(r"[,，]", aliases)
    if not isinstance(aliases, list):
        aliases = []
    names = [name, *[a for a in aliases if isinstance(a, str)]]
    names += [props.get(k) for k in ("user_name", "nickname") if isinstance(props.get(k), str)]
    return uid, [normalize(n) for n in names if n]


def rank(node, query, ids, terms):
    uid, names = identity(node)
    if node.get("label") == "Person" and ids:
        # 明确给了身份时，不用正文中的“提及”或 active_users 替代身份。
        return 1000 if uid in ids else 0
    normalized = normalize(query)
    if normalized in names:
        return 900
    if any(normalize(t) in names for t in terms):
        return 800
    if any(normalized in name for name in names if normalized):
        return 700
    if any(len(name) >= 2 and name in normalized for name in names):
        return 650
    if normalized and normalized in normalize(node.get("content")):
        return 300
    return 0


def _like(value):
    return "%" + value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def search_nodes(fetchall, query, label=None, group_id=None, limit=20):
    query, ids, terms = query_parts(query)
    if not query:
        return []
    limit = max(1, min(100, int(limit)))
    conditions, params = [], []
    if label:
        conditions.append("label = ?")
        params.append(label)
    # None 表示关闭群隔离；空字符串是一个实际私聊存储范围。
    if group_id is not None:
        conditions.append("group_id = ?")
        params.append(group_id)
    safe_json = "CASE WHEN json_valid(properties) THEN properties ELSE '{}' END"
    uid_sql = f"CAST(json_extract({safe_json}, '$.user_id') AS TEXT)"
    name_fields = ["name"] + [
        f"COALESCE(CAST(json_extract({safe_json}, '$.{field}') AS TEXT),'')"
        for field in ("user_name", "nickname")
    ]
    # 统一数组与旧版逗号分隔字符串，json_each 会解码 Unicode 转义。
    raw_aliases = f"json_extract({safe_json}, '$.aliases')"
    aliases_json = (
        f"CASE json_type({safe_json}, '$.aliases') "
        f"WHEN 'array' THEN {raw_aliases} "
        f"WHEN 'text' THEN '[' || REPLACE(json_quote(REPLACE({raw_aliases}, "
        "'，', ',')), ',', '\",\"') || ']' ELSE '[]' END"
    )

    def match_names(value):
        comparison = "LIKE ? ESCAPE '\\'"
        clauses = [f"{field} {comparison}" for field in name_fields]
        clauses.append(
            f"EXISTS (SELECT 1 FROM json_each({aliases_json}) AS alias "
            f"WHERE alias.type = 'text' AND TRIM(alias.value) {comparison})"
        )
        return "(" + " OR ".join(clauses) + ")", [_like(value)] * len(clauses)

    matches, match_params = [], []
    if ids and (not label or label == "Person"):
        # 有明确 QQ/用户 ID 时先定位身份，避免整句匹配和元数据泛匹配。
        for uid in ids:
            matches.append(f"(label = 'Person' AND (name = ? OR {uid_sql} = ? OR name = ? OR name LIKE ? ESCAPE '\\'))")
            match_params.extend([uid, uid, f"[用户:{uid}]", f"[用户:{uid}] %"])
        if not label:
            names_match, values = match_names(query)
            matches.append(
                f"(label <> 'Person' AND ({names_match} OR content LIKE ? ESCAPE '\\'))"
            )
            match_params.extend([*values, _like(query)])
    else:
        for term in list(dict.fromkeys([query, *terms])):
            names_match, values = match_names(term)
            matches.append(names_match)
            match_params.extend(values)
        matches.append("content LIKE ? ESCAPE '\\'")
        match_params.append(_like(query))
    conditions.append("(" + " OR ".join(matches) + ")")
    params.extend(match_params)

    # 分批读取所有匹配候选，再按同一 rank 规则保留 top-N。
    # 不在评分前截断候选，避免精确身份/别名被大量正文提及挤掉。
    # ID 为主键，用已读取的末行推进游标；即使整页评分为 0 也继续。
    base_query = (
        "SELECT id,label,name,content,confidence,group_id,properties FROM nodes WHERE "
        + " AND ".join(conditions)
    )
    ranked = []
    last_id = None
    while True:
        page_query = base_query
        page_params = list(params)
        if last_id is not None:
            page_query += " AND id > ?"
            page_params.append(last_id)
        rows = fetchall(page_query + " ORDER BY id LIMIT 2048", page_params)
        if not rows:
            break
        for row in rows:
            node = dict(row)
            node['content'] = str(node.get('content') or '')
            score = rank(node, query, ids, terms)
            if score:
                try:
                    confidence = float(node.get("confidence") or 0)
                except (ValueError, TypeError):
                    confidence = 0
                if not math.isfinite(confidence):
                    confidence = 0
                node['confidence'] = confidence
                ranked.append((-score, -confidence, str(node["id"]), node))
        # 最多仅持有一批候选和 limit 个保留结果，不累积整个图谱。
        ranked = heapq.nsmallest(limit, ranked, key=lambda item: item[:3])
        last_id = rows[-1]["id"]
        if len(rows) < 2048:
            break
    return [item[3] for item in ranked]
