"""图谱查询：稳定身份优先，只检索名称、正文与受控别名字段。"""
import json
import re
import unicodedata

_USER_ID = re.compile(r"(?<!\d)\d{5,20}(?!\d)")
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
    aliases_sql = " || ' ' || ".join(
        f"COALESCE(CAST(json_extract({safe_json}, '$.{field}') AS TEXT),'')"
        for field in ("aliases", "user_name", "nickname")
    )
    matches, match_params = [], []
    if ids and (not label or label == "Person"):
        # 有明确 QQ/用户 ID 时先定位身份，避免整句匹配和元数据泛匹配。
        for uid in ids:
            matches.append(f"(label = 'Person' AND (name = ? OR {uid_sql} = ? OR name = ? OR name LIKE ? ESCAPE '\\'))")
            match_params.extend([uid, uid, f"[用户:{uid}]", f"[用户:{uid}] %"])
        if not label:
            matches.append("(label <> 'Person' AND (name LIKE ? ESCAPE '\\' OR content LIKE ? ESCAPE '\\'))")
            match_params.extend([_like(query), _like(query)])
    else:
        for term in list(dict.fromkeys([query, *terms])):
            matches.append(f"(name LIKE ? ESCAPE '\\' OR ({aliases_sql}) LIKE ? ESCAPE '\\')")
            match_params.extend([_like(term), _like(term)])
        matches.append("content LIKE ? ESCAPE '\\'")
        match_params.append(_like(query))
    conditions.append("(" + " OR ".join(matches) + ")")
    params.extend(match_params)
    rows = fetchall(
        "SELECT id,label,name,content,confidence,group_id,properties FROM nodes WHERE "
        + " AND ".join(conditions)
        + " ORDER BY CASE WHEN name = ? COLLATE NOCASE THEN 0 ELSE 1 END, confidence DESC, id LIMIT 2048",
        [*params, query],
    )
    ranked = []
    for row in rows:
        node = dict(row)
        node['content'] = str(node.get('content') or '')
        score = rank(node, query, ids, terms)
        if score:
            try:
                confidence = float(node.get("confidence") or 0)
            except (ValueError, TypeError):
                confidence = 0
            node['confidence'] = confidence
            ranked.append((-score, -confidence, str(node["id"]), node))
    ranked.sort(key=lambda item: item[:3])
    return [item[3] for item in ranked[:limit]]
