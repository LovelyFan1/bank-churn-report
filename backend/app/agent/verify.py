"""保守核验实体、指标、单位和来源；无法映射的自然语言声明不展示。"""
import math
import re
from dataclasses import dataclass
from app.agent import answer_builder, quantities

_NUM_RE = re.compile(r"-?\d[\d,]*(?:\.\d+)?")
_CID_RE = re.compile(r"(?<![A-Za-z0-9])C\d{4,}(?![A-Za-z0-9])", re.I)
_ALIASES = {
    "probability": ("流失概率", "概率"), "balance": ("账户余额", "余额"),
    "expected_value": ("期望价值", "期望挽回", "风险资产评分"),
    "decision_threshold": ("决策阈值", "决策线"), "critical": ("极高危线", "极高线"),
    "high": ("高危线",), "medium": ("中等线",),
    "success_rate": ("挽留成功率", "成功率"), "model_recall": ("召回率",),
    "model_precision": ("精确率", "精准率"), "roi": ("ROI", "投入产出比"),
    "retained": ("已挽留人数", "挽留人数"), "lost": ("已流失人数",),
    "total": ("工单总数", "总数"), "returned": ("返回人数", "返回条数"),
    "matched_total": ("符合条件人数", "符合条件总数"),
    "cost_per_intervention": ("单次干预成本",), "cost_ratio": ("成本比",),
}
_RATIOS = {"probability", "success_rate", "model_recall", "model_precision", "decision_threshold", "critical", "high", "medium"}
_AMOUNTS = {"balance", "expected_value", "cost_per_intervention"}
_RISK_LABELS = {"CRITICAL": "CRITICAL", "HIGH": "HIGH", "MEDIUM": "MEDIUM", "LOW": "LOW",
    "极高危": "CRITICAL", "极高风险": "CRITICAL", "极高": "CRITICAL", "高危": "HIGH", "高风险": "HIGH",
    "中等风险": "MEDIUM", "中危": "MEDIUM", "中等": "MEDIUM", "低危": "LOW", "低风险": "LOW"}
_RISK_RE = re.compile("|".join(sorted(_RISK_LABELS, key=len, reverse=True)), re.I)
_ORDER_TOOLS = {"list_work_orders", "get_work_order"}


@dataclass(frozen=True)
class Fact:
    entity_type: str
    entity_id: str | None
    name: str | None
    metric: str
    value: float
    unit: str
    source: str
    model_version: str | None


def _facts(data, entity=None, name=None, source="", version=None):
    facts = []
    if isinstance(data, dict):
        entity = str(data["customer_id"]).upper() if data.get("customer_id") else entity
        if source in _ORDER_TOOLS and (data.get("order_id") or data.get("id")):
            entity = "#" + str(data.get("order_id") or data["id"])
        name = data.get("surname") or data.get("customer_name") or name
        version = data.get("model_version") or version
        for key, value in data.items():
            if key in _ALIASES and isinstance(value, (int, float)) and not isinstance(value, bool):
                if math.isfinite(value):
                    facts.append(Fact("order" if entity and entity.startswith("#") else
                        "customer" if entity else "aggregate", entity, name, key, float(value),
                        "fraction" if key in _RATIOS else "yuan" if key in _AMOUNTS else "number", source, version))
            elif isinstance(value, (dict, list)):
                facts.extend(_facts(value, entity, name, source, version))
    elif isinstance(data, list):
        for value in data:
            facts.extend(_facts(value, entity, name, source, version))
    return facts


def _entities(data, source):
    result = {}
    if isinstance(data, dict):
        if data.get("customer_id"):
            result[str(data["customer_id"]).upper()] = [data]
        if source in _ORDER_TOOLS and (data.get("order_id") or data.get("id")):
            result["#" + str(data.get("order_id") or data["id"])] = [data]
        for value in data.values():
            for entity, records in _entities(value, source).items():
                result.setdefault(entity, []).extend(records)
    elif isinstance(data, list):
        for value in data:
            for entity, records in _entities(value, source).items():
                result.setdefault(entity, []).extend(records)
    return result


def _mentions_name(name, clause):
    if not name:
        return False
    pattern = re.escape(str(name))
    if str(name).isascii():
        pattern = r"(?<![A-Za-z])" + pattern + r"(?![A-Za-z])"
    return re.search(pattern, clause) is not None


def verify(draft: str, tool_results: list[dict], extra_sources=None, question="") -> tuple[bool, list[str]]:
    if not draft or not draft.strip():
        return True, []
    facts, entities = [], {}
    for record in tool_results or []:
        if record.get("ok"):
            source = record.get("tool") or ""
            facts.extend(_facts(record.get("data"), source=source))
            for entity, rows in _entities(record.get("data"), source).items():
                entities.setdefault(entity, []).extend(rows)
    errors = []
    main = answer_builder.select_primary(tool_results, question)
    main_data = (main or {}).get("data") or {}
    main_items = main_data.get("items") or []
    count_source = len(main_items) if isinstance(main_items, list) and main else None
    if count_source is not None:
        facts = [f for f in facts if f.metric not in ('returned','matched_total')]
        facts.append(Fact('result',None,None,'returned',float(count_source),'count',(main or {}).get('tool',''),None))
        if isinstance(main_data.get('matched_total'), (int,float)):
            facts.append(Fact('result',None,None,'matched_total',float(main_data['matched_total']),'count',(main or {}).get('tool',''),None))
    # 人数及序号是主结果派生事实；辅助查询不能改变这一口径。
    def check_count(match):
        try:
            claimed = quantities.integer(match[2])
        except ValueError:
            errors.append("unsupported_numeric_expression")
            return ""
        ordinal = bool(match[1])
        prefix = match.string[max(0,match.start()-24):match.start()]
        returned_marker=max(prefix.rfind(word) for word in ('返回','展示','本页','本次','以下','推荐'))
        total_marker=max(prefix.rfind(word) for word in ('符合条件','匹配','共计','共有','总计','总人数'))
        expected = main_data.get('matched_total') if not ordinal and total_marker > returned_marker else count_source
        if expected is None or (not 1 <= claimed <= count_source if ordinal and count_source is not None else claimed != expected):
            errors.append("ordinal_mismatch" if ordinal else "returned_count_mismatch")
        return ""
    count_pattern = r"(第)?(" + quantities.TOKEN + r")\s*(?:位|人|个客户|名客户)"
    checked = re.sub(count_pattern, check_count, draft)
    mentioned_ids = [cid.upper() for cid in _CID_RE.findall(draft)] + re.findall(r"#\d+", draft)
    if any(entity not in entities for entity in mentioned_ids):
        errors.append("unknown_entity")
    # 中文数量和序号将在数量解析任务统一支持；未支持表达不能跳过核验。
    if re.search(r"百分之[零一二三四五六七八九十百千万两点]+|(?<![0-9.])[零一二三四五六七八九十百千万两]+(?:万元|元|人|位|项|倍)", checked):
        errors.append("unsupported_numeric_expression")
    cleaned = re.sub(r"(?<=\d),(?=\d{3}(?:\D|$))", "", checked)
    carry = set()
    for clause in re.split(r"([，,。；;\n])", cleaned):
        if clause in ("。", "；", ";", "\n"):
            carry = set()
            continue
        explicit = {cid.upper() for cid in _CID_RE.findall(clause)} | set(re.findall(r"#\d+", clause))
        named = {entity for entity, rows in entities.items() if any(
            _mentions_name(row.get("surname") or row.get("customer_name"), clause) for row in rows)}
        mentioned = explicit or named or carry
        if explicit or named:
            carry = explicit or named
        if explicit and named - explicit:
            # 工单声明中的客户姓名允许来自该工单本身，不能来自其他客户。
            expected_names = {row.get("surname") or row.get("customer_name")
                              for entity in explicit for row in entities.get(entity, [])}
            foreign_names = {row.get("surname") or row.get("customer_name")
                             for entity in named - explicit for row in entities.get(entity, [])}
            if foreign_names - expected_names:
                errors.append("entity_name_mismatch")
        claimed_name = re.search(r"(?:姓名|姓氏|客户名|姓)(?:是|为|叫|[:：])?\s*([^，。；;\s]+)", clause)
        if claimed_name and len(mentioned) == 1:
            names = {row.get("surname") or row.get("customer_name")
                     for row in entities.get(next(iter(mentioned)), [])}
            if claimed_name[1] not in names:
                errors.append("entity_name_mismatch")
        for label in _RISK_RE.finditer(clause):
            if clause[label.end():].startswith(("线", "阈值")):
                continue
            expected = _RISK_LABELS.get(label.group().upper(), _RISK_LABELS.get(label.group()))
            if mentioned and any(not entities.get(entity) or any(row.get("risk_level") != expected
                for row in entities[entity]) for entity in mentioned):
                errors.append("risk_label_mismatch")
        for match in _NUM_RE.finditer(clause):
            if match.start() and clause[match.start() - 1] in "Cc#":
                continue
            prefix = clause[:match.start()]
            aliases = [(prefix.rfind(alias), len(alias), key) for key, names in _ALIASES.items()
                       for alias in names if alias in prefix]
            if not aliases:
                errors.append("unsupported_number")
                continue
            metric = max(aliases)[2]
            candidates = [fact for fact in facts if fact.metric == metric]
            if mentioned:
                if len(mentioned) != 1:
                    errors.append("ambiguous_entity_mapping")
                    continue
                candidates = [fact for fact in candidates if fact.entity_id in mentioned]
            elif len(entities) > 1:
                candidates = [fact for fact in candidates if fact.entity_id is None]
            suffix = clause[match.end():].lstrip()
            factor = 1.0
            if suffix.startswith(("%", "％")):
                if metric not in _RATIOS:
                    errors.append("unit_mismatch")
                    continue
                factor = .01
            elif suffix.startswith("万"):
                if metric not in _AMOUNTS:
                    errors.append("unit_mismatch")
                    continue
                factor = 10000.0
            elif metric in _RATIOS and float(match.group().replace(",", "")) > 1:
                errors.append("unit_mismatch")
                continue
            if metric in _RATIOS and suffix.startswith(("元", "人", "位")) or suffix.startswith("元") and metric not in _AMOUNTS:
                errors.append("unit_mismatch")
                continue
            raw = match.group().replace(",", "")
            value = float(raw) * factor
            decimals = len(raw.split(".")[1]) if "." in raw else 0
            tolerance = max(1e-8, .5 * factor * 10 ** (-decimals))
            if metric in _RATIOS and factor == 1:
                tolerance = min(tolerance, .0005)
            if len({fact.value for fact in candidates}) > 1:
                errors.append("ambiguous_source_values")
            elif not any(abs(fact.value - value) <= tolerance for fact in candidates):
                errors.append("metric_value_mismatch")
    if re.search(r"非常值得|值得(?:安排|投入|人工)", draft) and not re.search(r"不值得|不建议|未必值得", draft):
        bad = any((record.get("data") or {}).get("worthiness", {}).get("verdict") in ("no_asset", "not_worth")
                  for record in tool_results if record.get("ok") and isinstance(record.get("data"), dict))
        if bad:
            errors.append("worthiness_mismatch")
    return not errors, list(dict.fromkeys(errors))


def explain(orphans: list[str]) -> str:
    return "模型解读中的部分声明未通过实体、指标与单位核对，已隐藏；请以数据卡片为准。" if orphans else ""
