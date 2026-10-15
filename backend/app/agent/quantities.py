"""受控整数解析：人数与序号分开，模糊数量不猜测。"""
import re

TOKEN = r"[零一二三四五六七八九十百两]+|\d+"
_DIGITS = {c: i for i, c in enumerate("零一二三四五六七八九")}
_DIGITS["两"] = 2


def integer(token):
    if token.isdigit():
        return int(token)
    if token in _DIGITS:
        return _DIGITS[token]
    if token == "一百" or token == "百":
        return 100
    if token.count("十") == 1:
        left, right = token.split("十")
        if (not left or left in _DIGITS) and (not right or right in _DIGITS):
            return (_DIGITS.get(left, 1) * 10) + _DIGITS.get(right, 0)
    raise ValueError("数量表达未支持，请使用明确整数")


def quantity(text, maximum=50):
    if re.search(r"(?:十几|几十|几)(?:个|位|名|人)", text):
        raise ValueError("请明确人数（1至50位），不使用‘十几位’等模糊数量")
    pattern = r"(?<![\d.零一二三四五六七八九十百两])(第)?([+-]?\d+(?:\.\d+)?|[零一二三四五六七八九十百两]+)\s*(?:位|名|人|个(?:客户)?)"
    for match in re.finditer(pattern, text):
        if match[1]:
            continue
        count = integer(match[2])
        if not 1 <= count <= maximum:
            raise ValueError(f"一次查询支持1至{maximum}位，请拆分更大的名单")
        return count
    return None


def ordinal(text, maximum=50):
    match = re.search(r"第(" + TOKEN + r")\s*(?:位|个|名)", text)
    if not match:
        return None
    count = integer(match[1])
    if not 1 <= count <= maximum:
        raise ValueError("序号超出当前名单能力，请使用客户编号")
    return count
