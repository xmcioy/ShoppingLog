/* ============================================================================
 * site/shared.js —— 展示层公共库
 *
 * 个人页 index.html 与公开广场 public.html **共用这一份**展示逻辑。
 * 以前两页各写一份，结果评分渲染漂移（个人页支持半星、公开页 Math.round 取整）、
 * 营养成分单位也不一致（千焦/克 vs kJ/g）。凡是两页都要显示的字段，
 * 一律在这里改一次，两边同时生效。
 *
 * 约束：
 *   1. 本文件不依赖登录态、不读接口、不碰页面私有全局（items / state 等），
 *      纯函数为主，两页都能安全加载。
 *   2. public.html 是免登录页，nginx 必须放行 /shared.js，否则公开广场白屏。
 *      也因此星级样式由本文件注入（只放行一个 js，不再多放行一个 css）。
 *   3. 导出到 window.SL，同时把常用函数直接挂到 window，页面里可裸写 esc(...) 调用；
 *      页面侧**不要**再重复定义同名函数，否则会遮蔽这里的版本，bug 又会潜伏。
 * ==========================================================================*/
(function (global) {
  "use strict";

  /* ---------------- 星级样式（含半星）---------------- */
  (function injectStarCss() {
    if (document.getElementById("sl-star-css")) return;
    var st = document.createElement("style");
    st.id = "sl-star-css";
    st.textContent =
      '.stars{display:inline-flex;font-size:13px;letter-spacing:1px;color:#ccc;line-height:1;vertical-align:middle}' +
      '.stars i{position:relative;font-style:normal}' +
      '.stars i.on{color:#f5b301}' +
      '.stars i.half{color:#ccc}' +
      '.stars i.half::after{content:"★";position:absolute;left:0;top:0;width:50%;overflow:hidden;color:#f5b301}';
    document.head.appendChild(st);
  })();

  /* ---------------- 基础工具 ---------------- */

  /** HTML 转义（含单引号，属性/文本都安全） */
  function esc(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  }

  /** 金额：1234.5 -> ¥1,234.50 */
  function money(v) {
    if (v == null || v === "") return "-";
    var n = Number(v);
    if (isNaN(n)) return "-";
    return "¥" + n.toLocaleString("zh-CN", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  }

  /** 小计 = 单价 × 数量 */
  function amt(i) { return (Number(i && i.price) || 0) * (Number(i && i.quantity) || 1); }

  /**
   * 星级渲染，支持半星。
   * 小数部分 >= 0.25 才画半星（避免 3.1 星显示成 3.5 星的误导）。
   * @param {number|string} n 评分 0-5
   * @returns {string} 已有 <span class="stars"> 包裹，外面不要再套一层
   */
  function starStr(n) {
    n = Number(n) || 0;
    if (!n) return "";
    if (n > 5) n = 5;
    var full = Math.floor(n), half = (n % 1) >= 0.25, s = "";
    for (var i = 0; i < 5; i++) {
      s += i < full
        ? '<i class="on">★</i>'
        : (i === full && half ? '<i class="half">★</i>' : "<i>★</i>");
    }
    return '<span class="stars">' + s + "</span>";
  }

  /** 头像首字母 */
  function initial(name) {
    var s = String(name == null ? "" : name).trim();
    return s ? s.slice(0, 1).toUpperCase() : "?";
  }

  /* ---------------- 营养 / 健康指数 ---------------- */

  /* 营养 NRV 基准（GB 28050）。单位统一用中文，两页一致。 */
  var NUTRI_LABEL = {
    energy_kj: "能量", protein_g: "蛋白质", fat_g: "脂肪", trans_fat_g: "反式脂肪酸",
    carbs_g: "碳水化合物", sugar_g: "糖", fiber_g: "膳食纤维", sodium_mg: "钠", calcium_mg: "钙"
  };
  var NUTRI_UNIT = {
    energy_kj: "千焦", protein_g: "克", fat_g: "克", trans_fat_g: "克",
    carbs_g: "克", sugar_g: "克", fiber_g: "克", sodium_mg: "毫克", calcium_mg: "毫克"
  };
  var NRV = {
    energy_kj: 8400, protein_g: 60, fat_g: 60, carbs_g: 300,
    fiber_g: 25, sodium_mg: 2000, calcium_mg: 800
  };
  var NUTRI_ORDER = ["energy_kj", "protein_g", "fat_g", "trans_fat_g", "carbs_g",
    "sugar_g", "fiber_g", "sodium_mg", "calcium_mg"];

  /**
   * 健康指数 0-100：按 NRV% 扣分（能量/碳水/糖/脂肪/钠）+ 加分（蛋白/钙/纤维）
   * @returns {number|null} 无营养数据时返回 null
   */
  function healthScore(n) {
    if (!n || Object.keys(n).length === 0) return null;
    function pct(k) { return (NRV[k] != null && n[k] != null) ? n[k] / NRV[k] * 100 : 0; }
    var penalty = Math.min(70,
      pct("energy_kj") * 0.4 + pct("carbs_g") * 0.5 + pct("sugar_g") * 1.0 +
      pct("fat_g") * 0.3 + pct("sodium_mg") * 0.6);
    var bonus = Math.min(30, pct("protein_g") * 0.4 + pct("calcium_mg") * 0.2 + pct("fiber_g") * 0.6);
    return Math.round(Math.max(0, Math.min(100, 100 - penalty + bonus)));
  }
  /* 档次阈值与后端 scripts/health_score.py 的 GRADE_BANDS 保持一致，
     改一处就得改另一处 —— 卡片上的 A/B/C/D 与筛选结果才不会打架。 */
  var GRADE_LABEL = { A: "优秀", B: "良好", C: "一般", D: "需注意" };
  function healthGrade(s) { return s >= 80 ? "A" : s >= 60 ? "B" : s >= 40 ? "C" : "D"; }

  /** @param {number|null} s 健康分；null 返回空串（不要给 null 上色） */
  function healthColor(s) {
    if (s == null || s === "") return "";
    s = Number(s);
    if (s >= 80) return "var(--good)";
    if (s >= 60) return "#2f9e6f";
    if (s >= 40) return "#e8935a";
    return "#e5484d";
  }

  /** 营养成分表（每 100g 含量 + NRV%） */
  function nutriTable(n) {
    if (!n || typeof n !== "object") return "";
    var keys = NUTRI_ORDER.filter(function (k) { return n[k] != null; });
    if (!keys.length) return "";
    var body = keys.map(function (k) {
      var v = n[k], pct = NRV[k] != null ? v / NRV[k] * 100 : null;
      var cls = pct == null ? "" : (pct >= 30 ? "hi" : pct >= 15 ? "mid" : "");
      return "<tr><td>" + (NUTRI_LABEL[k] || k) + "</td><td>" + v + " " + (NUTRI_UNIT[k] || "") + "</td>" +
        '<td class="' + cls + '">' + (pct == null ? "—" : Math.round(pct) + "%") + "</td></tr>";
    }).join("");
    return '<table class="nutri"><thead><tr><th>每 100g</th><th>含量</th><th>NRV%</th></tr>' +
      "<tbody>" + body + "</tbody></table>";
  }

  /** 营养摘要条：低脂/低糖/高碳水/高蛋白一眼可见 */
  function nutriSummary(n) {
    if (!n || Object.keys(n).length === 0) return "";
    var parts = [];
    if (n.fat_g != null) parts.push(n.fat_g <= 3 ? "<b>低脂 " + n.fat_g + "g</b>" : '<b class="warn">脂肪 ' + n.fat_g + "g</b>");
    if (n.sugar_g != null) parts.push(n.sugar_g <= 5 ? "<b>低糖</b>" : '<b class="warn">糖 ' + n.sugar_g + "g</b>");
    if (n.carbs_g != null) parts.push(n.carbs_g >= 60 ? '<b class="warn">高碳水 ' + n.carbs_g + "g</b>" : "<b>碳水 " + n.carbs_g + "g</b>");
    if (n.protein_g != null) parts.push(n.protein_g >= 15 ? "<b>高蛋白 " + n.protein_g + "g</b>" : "<b>蛋白 " + n.protein_g + "g</b>");
    if (n.calcium_mg != null) parts.push("钙 " + n.calcium_mg + "mg");
    return parts.length ? '<div class="nutri-sum">' + parts.join(" · ") + "</div>" : "";
  }

  /* ---------------- 导出 ---------------- */
  var api = {
    esc: esc, money: money, amt: amt, starStr: starStr, initial: initial,
    healthScore: healthScore, healthGrade: healthGrade, healthColor: healthColor,
    GRADE_LABEL: GRADE_LABEL,
    nutriTable: nutriTable, nutriSummary: nutriSummary,
    NUTRI_LABEL: NUTRI_LABEL, NUTRI_UNIT: NUTRI_UNIT, NRV: NRV, NUTRI_ORDER: NUTRI_ORDER
  };
  global.SL = api;
  /* 同时挂全局，页面里可裸写 esc() / starStr()，不必改存量调用点 */
  for (var k in api) { if (Object.prototype.hasOwnProperty.call(api, k)) global[k] = api[k]; }
})(window);
