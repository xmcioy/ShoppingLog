#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
本地 OCR 与字段解析：macOS Vision framework 文字识别 + 纯规则字段提取。
全程不联网、不调用任何模型 API。

对外函数:
  ocr_image(path) -> str          # 单张图片 OCR，返回多行文本
  parse_ocr_text(text) -> dict    # 从 OCR 全文提取购物记录字段
  ocr_fields(paths) -> dict       # 多张图片 → 合并字段
"""

import os
import re
import subprocess
from pathlib import Path

import shop  # 复用 NUTRI_ALIAS / parse_nutrition

APP_DEFAULT = Path(os.environ.get("SHOPLOG_APP", Path.home() / "myapp" / "shoppinglog"))
OCR_BIN_NAME = "ocr_vision"

# 广告/无关行过滤词（不参与名称猜测）
NOISE_WORDS = (
    "图片仅供参考", "仅供参考", "净含量", "保质期", "生产日期", "食用方法", "储存",
    "配料", "营养成分", "能量", "蛋白质", "脂肪", "碳水", "钠", "执行标准",
    "产品标准", "制造商", "地址", "电话", "热线", "图案仅供", "内置独立包装",
    "加量", "不加价", "升级", "新品尝", "扫描", "二维码", "条形码", "批号", "合格",
)


def ocr_bin():
    """定位已编译的 ocr_vision 二进制；不存在则用 swiftc 编译到运行目录 bin/。"""
    candidates = [
        Path(os.environ.get("SHOPLOG_APP", APP_DEFAULT)) / "bin" / OCR_BIN_NAME,
        Path(__file__).resolve().parent / OCR_BIN_NAME,
    ]
    for p in candidates:
        if p.is_file() and os.access(p, os.X_OK):
            return str(p)
    out = candidates[0]
    out.parent.mkdir(parents=True, exist_ok=True)
    src = Path(__file__).resolve().parent / "ocr_vision.swift"
    if not src.exists():
        return None
    subprocess.run(["swiftc", "-O", "-o", str(out), str(src)], check=True)
    return str(out)


def ocr_image(path):
    """单张图片本地 OCR。返回识别文本（多行）；失败返回空串。"""
    binp = ocr_bin()
    if not binp:
        return ""
    try:
        r = subprocess.run([binp, str(path)], capture_output=True, text=True, timeout=90)
        if r.returncode == 0:
            return r.stdout or ""
    except Exception:
        pass
    return ""


# --------------------------------------------------------------------------
# 字段解析（纯规则）
# --------------------------------------------------------------------------
DATE_RE = re.compile(r"(\d{4})[./\-年](\d{1,2})[./\-月](\d{1,2})日?")


def _norm_date(d):
    y, m, dd = d
    return f"{int(y):04d}-{int(m):02d}-{int(dd):02d}"


def _first_clean_name(lines):
    """取第一行像商品名称的文本（过滤广告/噪音行）"""
    for ln in lines:
        s = ln.strip().strip("·•*")
        if not s:
            continue
        if len(s) < 2 or len(s) > 40:
            continue
        if any(w in s for w in NOISE_WORDS):
            continue
        if re.fullmatch(r"[0-9¥￥元.，,：:（）()%]+", s):
            continue
        if re.fullmatch(r"[A-Za-z0-9\s.,\-&]+", s) and len(s) < 3:
            continue
        return s
    return None


def _extract_after(full, keys):
    """匹配 '键: 值' 形式，返回第一个非空值"""
    for k in keys:
        m = re.search(k + r"[:：\s]*([^\n|｜]{1,60})", full)
        if m:
            v = m.group(1).strip().strip("。；;")
            if v and not v.startswith(("无", "见", "印")):
                return v
    return None


# 标准营养键 → 中文显示名（用于 OCR 行匹配；"碳水化合物"含"碳水"子串可命中）
STD_NUTRI = {
    "energy_kj": ("能量", "热量"),
    "protein_g": ("蛋白质",),
    "fat_g": ("脂肪",),
    "sat_fat_g": ("饱和脂肪",),
    "carbs_g": ("碳水", "碳水化合物"),
    "sugar_g": ("糖",),
    "fiber_g": ("膳食纤维",),
    "sodium_mg": ("钠",),
}


def _extract_nutrition(text):
    """从 OCR 文本中收集营养成分行 → dict（每100g，标准键）"""
    out = {}
    for ln in text.splitlines():
        s = ln.strip().replace(" ", "")
        hit = None
        for std_key, names in STD_NUTRI.items():
            for nm in names:
                m = re.search(nm + r"[\s:：]*([\d.]+)", s)
                if m:
                    hit = std_key
                    try:
                        out[std_key] = float(m.group(1))
                    except ValueError:
                        pass
                    break
            if hit:
                break
    return out or None


def _extract_additives(ingredients):
    """统计配料中的添加剂/风险成分。返回 (数量, 命中列表)"""
    if not ingredients:
        return 0, []
    markers = [
        "山梨酸钾", "苯甲酸钠", "脱氢乙酸钠", "亚硝酸钠", "亚硫酸盐", "二氧化硫",
        "甜蜜素", "安赛蜜", "阿斯巴甜", "三氯蔗糖", "糖精钠", "纽甜",
        "柠檬黄", "日落黄", "胭脂红", "诱惑红", "苋菜红", "焦糖色", "亮蓝",
        "谷氨酸钠", "食用香精", "卡拉胶", "黄原胶", "瓜尔胶", "明胶",
        "氢化植物油", "起酥油", "人造奶油", "植脂末", "果葡糖浆",
        "山梨糖醇", "木糖醇", "麦芽糖醇", "赤藓糖醇",
    ]
    hits = [m for m in markers if m in ingredients]
    e_hits = re.findall(r"E\s?\d{3}[a-z]?", ingredients, re.I)
    for e in e_hits:
        e = re.sub(r"\s", "", e).upper()
        if e not in hits:
            hits.append(e)
    return len(hits), hits


def _guess_category(fields, text):
    """根据识别内容猜测分类：具备食品特征 → 食物；否则其他"""
    food_hints = [
        "配料", "营养成分", "能量", "蛋白质", "脂肪", "碳水", "钠", "膳食纤维",
        "净含量", "保质期", "食用方法", "g", "ml", "毫升", "克", "公斤", "kg",
        "薯条", "饮料", "牛奶", "蛋糕", "饼干", "面包", "糖果", "零食", "米", "面",
        "果汁", "茶", "酸奶", "冰淇淋", "水饺", "小笼包", "汤圆", "月饼",
    ]
    joined = f"{fields.get('name','')} {fields.get('brand','')} {text}"
    if any(h in joined for h in food_hints):
        return "食物"
    return "其他"


def parse_ocr_text(text):
    """从 OCR 全文提取字段。识别不到的字段不出现（由用户填写）。"""
    if not text or not text.strip():
        return {}
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    full = "\n".join(lines)
    fields = {}

    # 名称
    name = _extract_after(full, ("品名", "产品名称", "商品名称", "名称"))
    if not name:
        name = _first_clean_name(lines)
    if name:
        fields["name"] = name[:80]

    # 品牌 / 制造商
    brand = _extract_after(full, ("品牌", "制造商", "生产商", "出品", "委托方"))
    if brand:
        fields["brand"] = brand[:40]

    # 价格
    m = re.search(r"(?:价格|售价|实付|到手价|单价|现价)[:：\s]*[¥￥]?\s*(\d+(?:\.\d+)?)", full)
    if not m:
        m = re.search(r"[¥￥]\s*(\d+(?:\.\d+)?)", full)
    if m:
        fields["price"] = m.group(1)

    # 数量（净含量/规格）
    m = re.search(r"净含量[:：]?\s*(\d+(?:\.\d+)?)\s*(g|kg|克|千克|毫升|ml|l|斤)", full, re.I)
    if m:
        fields["quantity_note"] = f"{m.group(1)}{m.group(2)}"
        fields["quantity"] = "1"

    # 日期
    prod_m = re.search(r"(?:生产日期|生产日)[:：\s]*(\d{4})[./\-年](\d{1,2})[./\-月](\d{1,2})", full)
    if prod_m:
        fields["produced_date"] = _norm_date(prod_m.groups())
    exp_m = re.search(r"(?:保质期至|到期日期|有效期至|期至)[:：\s]*(\d{4})[./\-年](\d{1,2})[./\-月](\d{1,2})", full)
    if exp_m:
        fields["expiry_date"] = _norm_date(exp_m.groups())
    shelf_m = re.search(r"保质期[:：\s]*(\d+\s*(?:天|日|个月|月|年))", full)
    if shelf_m:
        fields["shelf_life"] = re.sub(r"\s+", "", shelf_m.group(1))
    if not fields.get("produced_date") and not fields.get("expiry_date"):
        # 兜底：任意日期 → 购买日期候选
        m = DATE_RE.search(full)
        if m:
            fields["purchase_date"] = _norm_date(m.groups())

    # 配料表
    ing = _extract_after(full, ("配料表", "配料"))
    if ing:
        # 去掉可能拼接的下一行开头（如 储存条件）
        ing = re.split(r"储存|贮存|食用方法|致敏|过敏原", ing)[0]
        fields["ingredients"] = ing[:300]
        add_n, add_hits = _extract_additives(ing)
        fields["additive_count"] = add_n
        fields["additives"] = add_hits

    # 营养成分
    nut = _extract_nutrition(full)
    if nut:
        fields["nutrition"] = nut

    # 分类
    fields["category"] = _guess_category(fields, full)
    return fields


def ocr_fields(paths):
    """多张图片 → 逐张 OCR → 合并字段（后图补充前图缺失项）"""
    merged = {}
    all_text = []
    for p in paths:
        t = ocr_image(p)
        if not t:
            continue
        all_text.append(t)
        f = parse_ocr_text(t)
        for k, v in f.items():
            if k not in merged or merged[k] in (None, "", [], 0):
                merged[k] = v
    merged["ocr_text"] = "\n".join(all_text)
    return merged
