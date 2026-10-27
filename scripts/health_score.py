#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
本地食品健康评分（纯规则算法，不依赖任何模型 API）。

依据（每 100g 基准）: 能量 / 蛋白质 / 脂肪 / 饱和脂肪 / 碳水 / 糖 /
膳食纤维 / 钠 + 配料表中添加剂数量与风险成分。

对外函数:
  score_food(nutrition=None, ingredients=None, additive_count=0, additives=None) -> dict
    返回 {"score": int, "level": str, "reasons": [str, ...], "summary": str}
"""

import re

# 配料中的高风险成分（减分项）
RISK_INGREDIENTS = {
    "氢化植物油": 10, "起酥油": 10, "人造奶油": 10, "植脂末": 10,  # 反式脂肪来源
    "果葡糖浆": 6, "白砂糖": 4, "葡萄糖浆": 4, "蔗糖": 3,          # 添加糖
    "谷氨酸钠": 3, "食用香精": 3, "山梨酸钾": 3, "苯甲酸钠": 3,
    "亚硝酸钠": 8, "甜蜜素": 6, "安赛蜜": 6, "阿斯巴甜": 6,
}

E_CODE_RE = re.compile(r"E\s?\d{3}[a-z]?", re.I)

# 档次表：分数下限 + 字母档 + 中文档名。
# 前后端共用同一套阈值（site/shared.js 的 healthGrade 与这里保持一致），
# 「档」是筛选维度（A/B/C/D），「档名」只是给人看的文案。
GRADE_BANDS = (
    (80, "A", "优秀"),
    (60, "B", "良好"),
    (40, "C", "一般"),
    (0, "D", "需注意"),
)


def grade_of(score):
    """分数 → (字母档, 中文档名)。score 为空返回 (None, None)。"""
    if score is None:
        return None, None
    for lo, letter, name in GRADE_BANDS:
        if score >= lo:
            return letter, name
    return "D", "需注意"


def band_of(letter):
    """字母档 → (下限, 上限, 档名)，用于 SQL 区间筛选。未知档位返回 None。"""
    up = None
    for i, (lo, lt, name) in enumerate(GRADE_BANDS):
        if lt == str(letter or "").upper():
            up = GRADE_BANDS[i - 1][0] if i > 0 else None
            return lo, up, name
    return None


def _cap(v, lo=0, hi=100):
    return max(lo, min(hi, int(round(v))))


def score_food(nutrition=None, ingredients=None, additive_count=0, additives=None):
    """本地健康评分。nutrition 为每 100g 的 dict（标准键：energy_kj/protein_g/fat_g/...）。"""
    nut = nutrition or {}
    ing = (ingredients or "") or ""
    reasons = []
    score = 70.0

    # ---- 能量 ----
    en = nut.get("energy_kj")
    if en is not None:
        if en >= 2000:
            score -= 10
            reasons.append("能量偏高（≥2000kJ/100g）")
        elif en >= 1500:
            score -= 5
            reasons.append("能量略高（≥1500kJ/100g）")

    # ---- 蛋白质（加分） ----
    pro = nut.get("protein_g")
    if pro is not None:
        if pro >= 10:
            score += 8
            reasons.append("蛋白质丰富（≥10g/100g）")
        elif pro >= 6:
            score += 5
            reasons.append("蛋白质含量尚可（≥6g/100g）")
        elif pro < 3:
            score -= 3
            reasons.append("蛋白质含量低（<3g/100g）")

    # ---- 脂肪 / 饱和脂肪 ----
    fat = nut.get("fat_g")
    if fat is not None:
        if fat > 20:
            score -= 12
            reasons.append("脂肪偏高（>20g/100g）")
        elif fat > 10:
            score -= 6
            reasons.append("脂肪略高（10–20g/100g）")
    sat = nut.get("sat_fat_g") or nut.get("trans_fat_g")
    if sat is not None and sat >= 5:
        score -= 8
        reasons.append("饱和脂肪较多（≥5g/100g）")

    # ---- 碳水 ----
    carb = nut.get("carbs_g")
    if carb is not None and carb > 60:
        score -= 4
        reasons.append("碳水化合物偏高（>60g/100g）")

    # ---- 糖 ----
    sugar = nut.get("sugar_g")
    if sugar is not None:
        if sugar > 15:
            score -= 12
            reasons.append("含糖量高（>15g/100g）")
        elif sugar > 5:
            score -= 6
            reasons.append("含糖量偏高（5–15g/100g）")

    # ---- 膳食纤维（加分） ----
    fiber = nut.get("fiber_g")
    if fiber is not None:
        if fiber >= 5:
            score += 10
            reasons.append("膳食纤维丰富（≥5g/100g）")
        elif fiber >= 3:
            score += 6
            reasons.append("含膳食纤维（≥3g/100g）")

    # ---- 钠 ----
    na = nut.get("sodium_mg")
    if na is not None:
        if na > 800:
            score -= 12
            reasons.append("钠含量高（>800mg/100g）")
        elif na > 500:
            score -= 8
            reasons.append("钠含量偏高（500–800mg/100g）")
        elif na > 300:
            score -= 4
            reasons.append("钠含量略高（300–500mg/100g）")

    # ---- 添加剂 ----
    add_hits = list(additives or [])
    if not add_hits and ing:
        add_hits = [m for m in RISK_INGREDIENTS if m in ing]
        e_hits = [re.sub(r"\s", "", e).upper() for e in E_CODE_RE.findall(ing)]
        add_hits.extend(e_hits)
    add_n = additive_count if additive_count is not None else len(add_hits)
    if not ing:
        # 未识别到配料表，无法评价添加剂，不加减分
        pass
    elif add_n == 0:
        score += 5
        reasons.append("未检出明显添加剂")
    elif add_n <= 1:
        reasons.append("含少量添加剂（1种）")
    elif add_n <= 4:
        score -= 5
        reasons.append(f"含多种添加剂（{add_n}种）")
    else:
        score -= 10
        reasons.append(f"添加剂较多（{add_n}种）")

    # ---- 配料风险成分 ----
    risk_penalty = 0
    for marker, p in RISK_INGREDIENTS.items():
        if marker in ing:
            risk_penalty += p
            reasons.append(f"含风险成分「{marker}」")
    score -= min(risk_penalty, 18)

    score = _cap(score, 5, 99)
    # 档位与档名统一走 GRADE_BANDS：前端 healthGrade() 用同一套阈值，
    # 卡片上的「A/B/C/D」与「优秀/良好/一般/需注意」永远一致。
    grade, level = grade_of(score)

    if not reasons:
        reasons.append("信息不足，按基础分计")
    summary = f"健康评分 {score}/100（{level}）：" + "、".join(reasons[:5]) + "。"
    return {"score": score, "level": level, "grade": grade,
            "reasons": reasons, "summary": summary}


# ============================================================
# 日化 / 通用成分健康评分（非食品，如美妆护肤等）
# 有配料表即可打分；识别高风险成分并提示，温和/功效成分加分。
# ============================================================

# 风险成分：扣分 + 提示（键名按成分表常见写法，匹配时按长度降序避免子串双扣）
COSMETIC_RISK = {
    "月桂醇聚醚硫酸酯钠": {"penalty": 10, "tip": "含硫酸盐表活（SLES），清洁力强但刺激性较高，头皮敏感或易脱发人群建议降低使用频率"},
    "月桂醇聚硫酸酯钠": {"penalty": 10, "tip": "含硫酸盐表活（SLES），清洁力强但刺激性较高，头皮敏感或易脱发人群建议降低使用频率"},
    "月桂醇硫酸酯钠": {"penalty": 10, "tip": "含硫酸盐表活（SLS），刺激性较高，敏感头皮慎用"},
    "甲基氯异噻唑啉酮": {"penalty": 12, "tip": "含 MIT/MCIT 强致敏防腐剂，敏感人群可能引起接触性皮炎，建议避开"},
    "甲基异噻唑啉酮": {"penalty": 12, "tip": "含 MIT 强致敏防腐剂，敏感人群可能引起接触性皮炎，建议避开"},
    "椰油酰胺dea": {"penalty": 6, "tip": "含椰油酰胺DEA，有潜在致敏/刺激风险，尽量避免"},
    "椰油酰胺mea": {"penalty": 5, "tip": "含椰油酰胺MEA，可能引起头皮刺激或致敏，敏感人群注意"},
    "水杨酸钠": {"penalty": 6, "tip": "含水杨酸盐，三岁以下儿童禁用，孕期/哺乳期慎用，避免长期大面积使用"},
    "水杨酸": {"penalty": 6, "tip": "含水杨酸，三岁以下儿童禁用，孕期/哺乳期慎用，避免长期大面积使用"},
    "苯氧乙醇": {"penalty": 4, "tip": "含苯氧乙醇（防腐剂），极少数人可能刺激"},
    "苯甲酸钠": {"penalty": 4, "tip": "含苯甲酸钠（防腐剂），酸性体系下释放苯甲酸，敏感人群注意"},
    "山梨酸钾": {"penalty": 3, "tip": "含山梨酸钾（防腐剂），一般较温和"},
    "焦糖色": {"penalty": 2, "tip": "含焦糖色（着色剂），无护理功效，仅为外观"},
    "香精": {"penalty": 4, "tip": "含香精，敏感头皮可能刺激，建议先做皮试"},
    "乙醇": {"penalty": 4, "tip": "含乙醇（酒精），干性/敏感头皮可能干燥刺激"},
}

# 温和 / 功效成分：加分
COSMETIC_BONUS = {
    "月桂酰谷氨酸钠": 5, "椰油酰甲基牛磺酸钠": 5, "椰油酰甘氨酸钾": 5,
    "氨基酸": 3, "侧柏": 4, "积雪草": 3, "透明质酸": 3, "燕麦": 2,
    "泛醇": 3, "烟酰胺": 2, "神经酰胺": 3, "角蛋白": 2, "何首乌": 3,
    "生姜": 2, "人参": 2, "生育酚": 2, "甘油": 1, "尿囊素": 2,
}


def score_cosmetic(ingredients=None):
    """非食品（日化/美妆等）成分健康评分。

    返回与 score_food 同构的 dict：{"score","level","grade","reasons","summary"}；
    无成分表返回 None（不硬凑分）。
    """
    if not ingredients or not str(ingredients).strip():
        return None
    text = str(ingredients).lower()
    score = 75.0
    tips = []
    # 先匹配更长的成分名，避免「水杨酸钠」命中后又叠加「水杨酸」
    matched = []
    for key, spec in sorted(COSMETIC_RISK.items(), key=lambda kv: len(kv[0]), reverse=True):
        if key in text and not any(key in m for m in matched):
            score -= spec["penalty"]
            tips.append(spec["tip"])
            matched.append(key)
    for key, bonus in COSMETIC_BONUS.items():
        if key in text:
            score += bonus
    score = _cap(score, 5, 99)
    grade, level = grade_of(score)
    if not tips:
        tips.append("未识别到明显高风险成分")
    reasons = tips[:5]
    summary = f"成分评分 {score}/100（{level}）：" + "、".join(reasons[:3]) + "。"
    return {"score": score, "level": level, "grade": grade,
            "reasons": reasons, "summary": summary}
