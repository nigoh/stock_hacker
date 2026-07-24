/* stock_hacker docs — 最小の共通スクリプト。
   1) テーマトグル（OS設定を既定に、選択は localStorage に保持）
   2) 一覧の絞り込み（data-filter 属性ベース。JS が無くても全件表示のまま） */
(function () {
  "use strict";

  /* --- テーマ ------------------------------------------------------------ */
  var root = document.documentElement;
  var KEY = "stock_hacker.theme";

  function stored() {
    try { return localStorage.getItem(KEY); } catch (e) { return null; }
  }
  function persist(v) {
    try { v ? localStorage.setItem(KEY, v) : localStorage.removeItem(KEY); } catch (e) { /* 保存できなくても動作は続ける */ }
  }
  function systemTheme() {
    return window.matchMedia && window.matchMedia("(prefers-color-scheme: light)").matches
      ? "light" : "dark";
  }
  function current() {
    return root.getAttribute("data-theme") || stored() || systemTheme();
  }
  function apply(theme) {
    root.setAttribute("data-theme", theme);
    persist(theme);
    document.querySelectorAll("[data-theme-toggle]").forEach(function (btn) {
      btn.textContent = theme === "dark" ? "◐ light" : "◑ dark";
      btn.setAttribute("aria-label", theme === "dark" ? "ライトテーマに切り替える" : "ダークテーマに切り替える");
    });
  }

  var saved = stored();
  if (saved === "dark" || saved === "light") { apply(saved); }
  else { apply(systemTheme()); }

  document.addEventListener("click", function (ev) {
    var btn = ev.target.closest && ev.target.closest("[data-theme-toggle]");
    if (!btn) return;
    apply(current() === "dark" ? "light" : "dark");
  });

  /* --- 絞り込み ---------------------------------------------------------- */
  /* 使い方:
       <input class="field" data-filter-input="cli">
       <button class="tab" data-filter-tab="cli" data-value="all" aria-pressed="true">ALL</button>
       <tr data-filter-item="cli" data-cat="tech" data-text="analyze_stock 個別分析">
       <p data-filter-empty="cli" hidden>該当なし</p>
       <span data-filter-count="cli"></span>                                */
  function groups() {
    var set = {};
    document.querySelectorAll("[data-filter-item]").forEach(function (el) {
      set[el.getAttribute("data-filter-item")] = true;
    });
    return Object.keys(set);
  }

  groups().forEach(function (name) {
    var sel = '[data-filter-item="' + name + '"]';
    var items = Array.prototype.slice.call(document.querySelectorAll(sel));
    var input = document.querySelector('[data-filter-input="' + name + '"]');
    var tabs = Array.prototype.slice.call(document.querySelectorAll('[data-filter-tab="' + name + '"]'));
    var empty = document.querySelector('[data-filter-empty="' + name + '"]');
    var count = document.querySelector('[data-filter-count="' + name + '"]');
    var total = items.length;
    var cat = "all";

    function run() {
      var q = (input && input.value ? input.value : "").trim().toLowerCase();
      var shown = 0;
      items.forEach(function (el) {
        var text = (el.getAttribute("data-text") || el.textContent || "").toLowerCase();
        var itemCat = el.getAttribute("data-cat") || "";
        var okCat = cat === "all" || itemCat === cat;
        var okText = !q || text.indexOf(q) !== -1;
        var ok = okCat && okText;
        el.hidden = !ok;
        if (ok) shown++;
      });
      if (empty) empty.hidden = shown !== 0;
      if (count) count.textContent = shown === total ? String(total) : shown + "/" + total;
    }

    if (input) {
      input.addEventListener("input", run);
      input.addEventListener("search", run);
    }
    tabs.forEach(function (tab) {
      tab.addEventListener("click", function () {
        cat = tab.getAttribute("data-value") || "all";
        tabs.forEach(function (t) { t.setAttribute("aria-pressed", String(t === tab)); });
        run();
      });
    });
    run();
  });
})();
