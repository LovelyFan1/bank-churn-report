"""查询签名只包含业务筛选与排序，分页由服务端上下文决定。"""
import re
import math
from app.agent.quantities import integer, ordinal, quantity

DEFAULTS = {"sort_by": "expected_value", "exited": 0, "has_order": False, "worth_only": False}
FILTERS = set(DEFAULTS) | {"geography", "risk_level", "value_tier", "min_balance", "max_balance"}
FOLLOWUP = re.compile(r"另外|下一批|下一页|再来|再调|还有吗|继续(?:列出|查询|推荐|调出|找|查|看|下一批)")
_REGIONS = {"France": "France", "法国": "France", "Germany": "Germany",
            "德国": "Germany", "Spain": "Spain", "西班牙": "Spain"}
_RISKS = {"极高危": "CRITICAL", "极高风险": "CRITICAL", "高危": "HIGH", "高风险": "HIGH",
          "中等风险": "MEDIUM", "中危": "MEDIUM", "低危": "LOW", "低风险": "LOW"}


def _choice(question, labels, description):
    positive, negative = set(), set()
    lookup = {key.lower(): value for key, value in labels.items()}
    pattern = "|".join(re.escape(key) for key in sorted(labels, key=len, reverse=True))
    for match in re.finditer(pattern, question, re.I):
        prefix = question[max(0, match.start() - 16):match.start()]
        negated = re.search(r"(?:不要|不看|不选|不包括|不包含|不需要|不查询|不是|排除|除了|非|别)"
                            r"\s*(?:任何|所有|属于|的)?\s*$", prefix)
        (negative if negated else positive).add(lookup[match[0].lower()])
    if positive & negative or len(positive) > 1:
        raise ValueError(f"{description}条件无法用单一筛选表达，请指定一个明确选项。")
    if not positive and negative:
        raise ValueError(f"暂不支持仅排除{description}，请指定需要的选项或取消该限制。")
    return next(iter(positive), None)


def _balance_changes(question):
    if re.search(r"不限(?:制)?余额|不限制余额|取消余额(?:筛选|限制)", question):
        return {"min_balance": None, "max_balance": None}
    number = r"(\d+(?:\.\d+)?|[零一二三四五六七八九十百两]+)\s*(万)?\s*(?:元)?"
    segments = re.findall(r"余额[^，,。；;\n]*", question)
    if any(re.search(r"或者|或|最多|最少", segment) for segment in segments):
        raise ValueError("余额条件请使用明确的上下限，例如‘余额至少20万且至多30万’。")
    if re.search(r"余额\s*(?:在|介于|为|范围)?\s*" + number + r"\s*(?:到|至|[-~－]|和)", question):
        raise ValueError("余额区间请写成‘余额至少20万且至多30万’，避免遗漏边界。")
    lower = re.search(r"(?:余额|且|并且|同时|以及)\s*(?:至少|不低于|不少于|大于等于|>=|≥)\s*" + number, question)
    upper = re.search(r"(?:余额|且|并且|同时|以及)\s*(?:至多|不高于|不超过|小于等于|<=|≤)\s*" + number, question)
    strict_lower = re.search(r"(?:余额|且|并且|同时|以及)\s*(?:超过|高于|大于(?!等于)|>(?!=))\s*" + number, question)
    strict_upper = re.search(r"(?:余额|且|并且|同时|以及)\s*(?:低于|小于(?!等于)|<(?!=))\s*" + number, question)
    suffix = re.search(r"余额\s*" + number + r"\s*(以上|以下)", question)
    changes = {}
    for key, match, direction in (("min_balance", lower, None), ("max_balance", upper, None),
            ("min_balance", strict_lower, math.inf), ("max_balance", strict_upper, -math.inf)):
        if match:
            value = float(match[1]) if match[1][0].isdigit() else integer(match[1])
            value *= 10000 if match[2] else 1
            # 查询API使用含端点比较；相邻浮点值精确表达当前float余额的严格边界。
            if direction is not None:
                value = math.nextafter(value, direction)
            if not math.isfinite(value) or value < 0:
                raise ValueError("余额边界没有有效的非负范围，请调整筛选条件。")
            combine = max if key == "min_balance" else min
            changes[key] = combine(changes[key], value) if key in changes else value
    if suffix:
        value = float(suffix[1]) if suffix[1][0].isdigit() else integer(suffix[1])
        changes["min_balance" if suffix[3] == "以上" else "max_balance"] = value * (10000 if suffix[2] else 1)
    if not changes and (re.search(r"余额.{0,8}(?:至少|至多|[<>≥≤]|大于|小于|超过|低于|高于|以上|以下|不少|不低|不超)", question)
                        or re.search(r"余额\s*(?:在|介于|为|范围)?\s*" + number, question)):
        raise ValueError("余额条件暂不支持该表达，请使用‘余额至少50万’或‘余额至多50万’。")
    return changes


def signature(params):
    return {key: params.get(key, DEFAULTS.get(key)) for key in sorted(FILTERS)}


def requested_changes(question):
    changes = {}
    if re.search(r"不限(?:制)?地区|不限制地区|所有地区|全部地区|不分地区|取消地区(?:筛选|限制)", question):
        changes['geography'] = None
    else:
        region = _choice(question, _REGIONS, "地区")
        if region:
            changes['geography'] = region
    if re.search(r"不限(?:制)?风险|不限制风险|所有风险等级|取消风险(?:筛选|限制)", question):
        changes['risk_level'] = None
    else:
        risk = _choice(question, _RISKS, "风险等级")
        if risk:
            changes['risk_level'] = risk
    changes.update(_balance_changes(question))
    if ("年龄" in question and "按年龄" not in question) or any(
            word in question for word in ("性别", "产品数量", "产品数大于", "投诉次数", "任职年限")):
        raise ValueError("当前名单工具不支持该筛选字段，请使用地区、风险、余额或工单条件。")
    for words, value in ((('价值最高', '挽回价值', '期望价值', '风险资产评分'), 'expected_value'), (('概率最高', '按概率', '风险最高'), 'probability'), (('余额最高', '按余额'), 'balance'), (('按年龄',), 'age'), (('按信用评分',), 'credit_score')):
        if any(word in question for word in words):
            changes['sort_by'] = value
    if '未流失' in question or '没有流失' in question:
        changes['exited'] = 0
    elif '已流失' in question:
        changes['exited'] = 1
    if any(word in question for word in ('没有工单', '没有进行中工单', '没有在途工单', '未建单')):
        changes['has_order'] = False
    elif '已有工单' in question:
        changes['has_order'] = True
    if '值得' in question and any(word in question for word in ('人工', '投入', '挽留')):
        changes['worth_only'] = True
    return changes


def is_customer_continuation(question):
    """A customer cursor cannot paginate work orders, details or another topic."""
    if not FOLLOWUP.search(question) or ordinal(question) is not None:
        return False
    remaining = re.sub(r"(?:没有(?:进行中|在途)?|已有|没有|未建)(?:的)?工单", "", question)
    remaining = re.sub(r"(?:不(?:创建|建立)|不要(?:创建|建立)|不建)(?:挽留)?工单", "", remaining)
    if "工单" in remaining:
        return False
    return not any(word in remaining for word in ("阈值", "成本", "能做什么", "哪些功能", "你是谁", "回访结果", "挽留结果"))


def is_global_retention_summary(question, ctx=None):
    if "实际" not in question or not re.search(r"挽留|回访", question):
        return False
    if re.search(r"C\d{4,}|第[\d一二三四五六七八九十两]+|这位|那位|这批|那批|这些|他|她|该客户|这个客户|当前客户|该工单|哪个|哪些|谁", question, re.I):
        return False
    if ctx and any(ctx.get(key) for key in ("focus_customer_id", "displayed_customer_ids", "customers", "orders")):
        if not re.search(r"全局|全行|全部(?:工单|客户)|所有(?:工单|客户)", question):
            return False
    return bool(re.search(r"汇总|统计|成功率|结果|情况|效果|多少", question))


def continuation(previous, question, proposed=None):
    params = {key: value for key, value in previous.items() if key in FILTERS | {'limit'}}
    params.update({key: value for key, value in (proposed or {}).items() if key in FILTERS | {'limit'}})
    params.update(requested_changes(question))
    size = quantity(question)
    if size is not None:
        params['limit'] = size
    if (params.get('min_balance') is not None and params.get('max_balance') is not None
            and params['min_balance'] > params['max_balance']):
        raise ValueError("余额上下限冲突，请明确新的完整余额范围。")
    changed = signature(params) != signature(previous)
    params['offset'] = 0 if changed else previous.get('next_offset', 0)
    if not changed:
        params['exclude_ids'] = previous.get('seen_customer_ids') or []
        params['expected_version'] = previous.get('model_version')
    return params, changed
