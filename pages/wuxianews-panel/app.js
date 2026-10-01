(function () {
  "use strict";

  var state = {
    meta: {},
    status: {},
    groups: [],        // 主表格：{umo,label,group_id,group_name,enabled,last_pushed}
    pickGroups: [],    // 「选择群」弹窗：{value,label,group_id,group_name,enabled,source}
    picked: {},        // 弹窗里的勾选状态：umo -> bool
    pickLoaded: false,
    records: [],
    shots: [],
    testTarget: null
  };

  // ---------- 小工具 ----------

  function byId(id) { return document.getElementById(id); }

  var toastTimer = null;
  function toast(msg, isErr) {
    var el = byId("toast");
    el.textContent = String(msg || "");
    el.className = "toast show" + (isErr ? " err" : "");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(function () { el.className = "toast"; }, 3200);
  }

  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }

  // 面板跑在 AstrBot 仪表盘的 iframe 里，浏览器会屏蔽 window.prompt/confirm，
  // 所以用页内弹窗替代（否则确认框点了没反应）。
  function _modal(opts) {
    return new Promise(function (resolve) {
      var mask = byId("modalMask");
      var input = byId("modalInput");
      byId("modalTitle").textContent = opts.title || "";
      byId("modalMsg").textContent = opts.message || "";
      input.hidden = !opts.prompt;
      if (opts.prompt) { input.value = opts.value || ""; }
      mask.hidden = false;
      var previousFocus = document.activeElement;
      byId("modalOk").focus();
      function cancelOnEscape(e) {
        if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); done(opts.prompt ? null : false); }
      }
      mask.addEventListener("keydown", cancelOnEscape);
      if (opts.prompt) { input.focus(); input.select(); }
      function done(val) {
        mask.hidden = true;
        mask.removeEventListener("keydown", cancelOnEscape);
        if (previousFocus && previousFocus.focus) previousFocus.focus();
        byId("modalOk").onclick = null;
        byId("modalCancel").onclick = null;
        input.onkeydown = null;
        resolve(val);
      }
      byId("modalOk").onclick = function () { done(opts.prompt ? input.value : true); };
      byId("modalCancel").onclick = function () { done(opts.prompt ? null : false); };
      input.onkeydown = function (e) {
        if (e.key === "Enter") { e.preventDefault(); done(input.value); }
        else if (e.key === "Escape") { e.preventDefault(); done(null); }
      };
    });
  }
  function promptModal(title, message, value) {
    return _modal({ prompt: true, title: title, message: message, value: value || "" });
  }
  function confirmModal(message, title) {
    return _modal({ prompt: false, title: title || "确认", message: message });
  }

  function fmtBytes(n) {
    n = Number(n) || 0;
    if (n >= 1048576) return (n / 1048576).toFixed(1) + " MB";
    if (n >= 1024) return (n / 1024).toFixed(0) + " KB";
    return n + " B";
  }

  // 后端统一返回 {status, message, data}；status 不是 ok 就抛出 message。
  function unwrap(resp) {
    if (resp && typeof resp === "object" && "status" in resp) {
      if (String(resp.status).toLowerCase() !== "ok") {
        throw new Error(resp.message || "请求失败");
      }
      return resp.data || {};
    }
    return resp || {};
  }

  var _bridgeReady = null;
  function waitForBridge(timeoutMs) {
    if (_bridgeReady) return _bridgeReady;
    _bridgeReady = new Promise(function (resolve, reject) {
      var pollTimer;
      var expired = false;
      var timeout = setTimeout(function () {
        expired = true;
        clearTimeout(pollTimer);
        reject(new Error("面板桥接不可用，请在 AstrBot WebUI 里打开本页面"));
      }, timeoutMs || 8000);
      function finish(error, bridge) {
        if (expired) return;
        clearTimeout(timeout);
        if (error) reject(error);
        else resolve(bridge);
      }
      (function poll() {
        var bridge = window.AstrBotPluginPage;
        if (!bridge) { pollTimer = setTimeout(poll, 32); return; }
        Promise.resolve().then(function () {
          if (typeof bridge.ready === "function") return bridge.ready();
        }).then(function () { finish(null, bridge); }, function (err) { finish(err); });
      })();
    }).catch(function (err) {
      _bridgeReady = null;
      throw err;
    });
    return _bridgeReady;
  }

  function apiGet(path, params) {
    return window.PanelUI.request(waitForBridge().then(function (bridge) {
      return bridge.apiGet(path, params || {}).then(unwrap);
    }));
  }

  function apiPost(path, body) {
    return window.PanelUI.request(waitForBridge().then(function (bridge) {
      return bridge.apiPost(path, body || {}).then(unwrap);
    }));
  }

  function fail(err) { toast((err && err.message) || "操作失败", true); }

  function initTheme() { /* Theme is owned by shell.js. */ }

  // ---------- 渲染 ----------

  function renderStatus() {
    var st = state.status || {};
    var pill = byId("runPill");
    var hasGroups = (st.groups || []).length > 0;
    if (!hasGroups) {
      pill.textContent = "还没有推送群";
      pill.className = "pill warn";
    } else if (st.running) {
      pill.textContent = "定时推送运行中";
      pill.className = "pill";
    } else {
      pill.textContent = "定时任务未启动";
      pill.className = "pill off";
    }
    byId("stats").innerHTML = [
      '<div class="stat"><div class="n">' + esc((st.groups || []).length) + '</div><div class="l">推送群</div></div>',
      '<div class="stat"><div class="n">' + esc(st.last_check_text || "从未") + '</div><div class="l">上次检查</div></div>',
      '<div class="stat"><div class="n">' + (hasGroups ? (st.next_check_in || 0) + " 秒" : "—") + '</div><div class="l">距下次检查</div></div>',
      '<div class="stat"><div class="n">' + esc(st.shot_count || 0) + '</div><div class="l">截图缓存（' + fmtBytes(st.shot_bytes) + '）</div></div>',
      '<div class="stat"><div class="n">' + esc(st.summary_cache_count || 0) + '</div><div class="l">摘要缓存</div></div>',
      '<div class="stat"><div class="n">' + esc(st.record_count || 0) + '</div><div class="l">推送记录</div></div>'
    ].join("");
  }

  function renderGroups() {
    var tb = byId("groupBody");
    if (!state.groups.length) {
      tb.innerHTML = '<tr><td colspan="5" class="empty">还没有推送群：点右上角「选择群…」，' +
        '或在群里发「天刀新闻推送 开」</td></tr>';
      return;
    }
    tb.innerHTML = state.groups.map(function (g) {
      return "<tr>" +
        '<td><input type="checkbox" data-act="check" data-umo="' + esc(g.umo) + '"' +
          (g.enabled ? " checked" : "") + "></td>" +
        "<td>" + esc(g.group_name || g.label) +
          (g.group_name ? "" : ' <span class="tag warn">无群名</span>') + "</td>" +
        '<td class="nm">' + esc(g.group_id || "—") + "</td>" +
        "<td>" + (g.last_pushed ? esc(g.last_pushed) : '<span class="hint">还没推过</span>') + "</td>" +
        '<td class="acts">' +
          '<button class="link" data-act="test" data-umo="' + esc(g.umo) + '">测试推送</button>' +
          '<button class="link danger" data-act="reset" data-umo="' + esc(g.umo) + '">重置记录</button>' +
        "</td></tr>";
    }).join("");
  }

  function renderRecords() {
    var box = byId("recordList");
    if (!state.records.length) {
      box.innerHTML = '<div class="empty">还没有推送记录（第一次推送成功后会出现）</div>';
      byId("recordHint").textContent = "";
      return;
    }
    box.innerHTML = state.records.map(function (r) {
      return '<div class="list-item"><span class="nm" title="' + esc(r.title) + '">' +
        esc(r.label) + " · " + esc(r.title || "(空)") + "</span>" +
        (r.umo ? '<button class="link" data-act="reset-one" data-umo="' + esc(r.umo) + '">重置</button>' : "") +
        "</div>";
    }).join("");
    byId("recordHint").textContent = "共 " + state.records.length + " 条";
  }

  function renderShots() {
    var box = byId("gallery");
    var total = 0;
    state.shots.forEach(function (s) { total += Number(s.size) || 0; });
    byId("shotHint").textContent = state.shots.length
      ? "共 " + state.shots.length + " 张，占用 " + fmtBytes(total) + "（保留最近 20 张 / 7 天）"
      : "还没有截图缓存（推送过一次公告后会有）";
    if (!state.shots.length) { box.innerHTML = ""; return; }
    box.innerHTML = state.shots.map(function (s) {
      var inner = s.url
        ? '<img src="' + esc(s.url) + '" alt="' + esc(s.name) + '" loading="lazy">'
        : '<div class="cap">（无法预览，只有文件名）</div>';
      return '<div class="thumb">' + inner +
        '<div class="cap">' + esc(s.modified_text) + "<br>" + fmtBytes(s.size) + "<br>" +
        esc(s.name) + "</div></div>";
    }).join("");
  }

  function renderConfig() {
    var cfg = (state.meta || {}).config || {};
    byId("cfgShotEnabled").value = cfg.shot_enabled === false ? "0" : "1";
    byId("cfgShotHeight").value = cfg.shot_max_height == null ? "5000" : cfg.shot_max_height;
    byId("cfgShotQuality").value = cfg.shot_quality == null ? "88" : cfg.shot_quality;
  }

  // ---------- 数据加载 ----------

  function loadMeta() {
    return apiGet("page/meta").then(function (d) {
      state.meta = d || {};
      renderConfig();
    });
  }

  function loadStatus() {
    return apiGet("page/status").then(function (d) {
      state.status = d || {};
      state.groups = (d.groups || []).map(function (g) {
        return {
          umo: g.umo, label: g.label, group_id: g.group_id,
          group_name: g.group_name || "", enabled: true,
          last_pushed: g.last_pushed || ""
        };
      });
      renderStatus();
      renderGroups();
    });
  }

  function loadRecords() {
    return apiGet("page/records").then(function (d) {
      state.records = d.records || [];
      renderRecords();
    });
  }

  function loadShots() {
    return apiGet("page/shots").then(function (d) {
      state.shots = d.shots || [];
      renderShots();
    });
  }

  function loadAll() {
    return Promise.all([loadMeta(), loadStatus(), loadRecords(), loadShots()]);
  }

  // ---------- 保存推送群 ----------

  function checkedUmOs() {
    var boxes = byId("groupBody").querySelectorAll('input[type="checkbox"][data-act="check"]');
    var out = [];
    Array.prototype.forEach.call(boxes, function (box) {
      if (box.checked) out.push(box.getAttribute("data-umo"));
    });
    return out;
  }

  function saveGroups() {
    var umos = checkedUmOs();
    var btn = byId("btnSaveGroups");
    btn.disabled = true;
    byId("saveHint").textContent = "保存中…";
    apiPost("page/groups/save", { groups: umos }).then(function () {
      byId("saveHint").textContent = "";
      return loadAll().then(function () { toast("已保存 " + umos.length + " 个推送群"); });
    }).catch(fail).then(function () {
      btn.disabled = false;
      byId("saveHint").textContent = "";
    });
  }

  // ---------- 手动推送 ----------

  function pushAll() {
    var btn = byId("btnPushAll");
    btn.disabled = true;
    byId("saveHint").textContent = "正在检查公告…";
    apiPost("page/push-now", {}).then(function (d) {
      byId("saveHint").textContent = "";
      toast(d.message || "已完成", d.reason !== "ok" && d.reason !== "up_to_date");
      return loadAll();
    }).catch(fail).then(function () {
      btn.disabled = false;
      byId("saveHint").textContent = "";
    });
  }

  function openTest(umo) {
    var row = null;
    for (var i = 0; i < state.groups.length; i++) {
      if (state.groups[i].umo === umo) { row = state.groups[i]; }
    }
    state.testTarget = umo;
    byId("testTarget").textContent = "推送到：" + (row ? (row.group_name || row.label) : umo) +
      "（" + umo + "）";
    byId("testResult").textContent = "";
    byId("testForce").checked = true;
    byId("testMask").hidden = false;
  }

  function runTest() {
    if (!state.testTarget) return;
    var btn = byId("btnTestRun");
    btn.disabled = true;
    byId("testResult").textContent = "正在推送…";
    apiPost("page/push-now", {
      umo: state.testTarget,
      force: byId("testForce").checked
    }).then(function (d) {
      byId("testResult").textContent = d.message || "已完成";
      toast(d.message || "已完成", !(d.pushed || []).length);
      return loadAll();
    }).catch(function (err) {
      byId("testResult").textContent = (err && err.message) || "推送失败";
      fail(err);
    }).then(function () { btn.disabled = false; });
  }

  // ---------- 推送记录 ----------

  function resetRecords(umo) {
    var tip = umo ? "重置这个群的推送记录？" : "清空全部推送记录？下次检查会重推最新公告。";
    confirmModal(tip, "重置推送记录").then(function (ok) {
      if (!ok) return;
      apiPost("page/records/reset", umo ? { umo: umo } : {}).then(function () {
        return loadAll().then(function () { toast("已重置"); });
      }).catch(fail);
    });
  }

  // ---------- 缓存 / 参数 ----------

  function clearShots() {
    confirmModal("清空截图缓存？下次推送会重新截图。", "清空截图缓存").then(function (ok) {
      if (!ok) return;
      apiPost("page/shots/clear", {}).then(function (d) {
        return loadAll().then(function () { toast(d.message || "已清空"); });
      }).catch(fail);
    });
  }

  function clearCache() {
    apiPost("page/cache/clear", {}).then(function (d) {
      return loadAll().then(function () { toast(d.message || "已清空"); });
    }).catch(fail);
  }

  function saveConfig() {
    var btn = byId("btnSaveConfig");
    btn.disabled = true;
    byId("cfgHint").textContent = "保存中…";
    apiPost("page/config/save", {
      shot_enabled: byId("cfgShotEnabled").value === "1",
      shot_max_height: byId("cfgShotHeight").value.trim(),
      shot_quality: byId("cfgShotQuality").value.trim()
    }).then(function () {
      byId("cfgHint").textContent = "";
      return loadMeta().then(function () { toast("参数已保存"); });
    }).catch(fail).then(function () {
      btn.disabled = false;
      byId("cfgHint").textContent = "";
    });
  }

  // ---------- 选择群弹窗 ----------

  function loadPickGroups(refresh) {
    return apiGet("page/groups", refresh ? { refresh: "1" } : {}).then(function (d) {
      state.pickGroups = d.groups || [];
      state.pickLoaded = true;
      // 用当前表格的勾选状态初始化弹窗勾选（表格里没有的按后端 enabled）
      var current = {};
      checkedUmOs().forEach(function (u) { current[u] = true; });
      var next = {};
      state.pickGroups.forEach(function (g) {
        next[g.value] = current[g.value] === undefined ? !!g.enabled : !!current[g.value];
      });
      state.picked = next;
      renderPickList();
      byId("pickHint").textContent = d.total
        ? "共 " + d.total + " 个会话，当前选中 " + countPicked() + " 个"
        : "还没有可选会话：点「刷新群列表」或先在群里说句话";
      if (refresh) toast("群列表已刷新（" + (d.refreshed || 0) + " 个群有更新）");
      return state.pickGroups;
    });
  }

  function countPicked() {
    var n = 0;
    Object.keys(state.picked).forEach(function (k) { if (state.picked[k]) n++; });
    return n;
  }

  function openPick() {
    byId("pickSearch").value = "";
    byId("pickMask").hidden = false;
    renderPickList();
    byId("pickSearch").focus();
    if (!state.pickLoaded) loadPickGroups(false).catch(fail);
  }

  function renderPickList() {
    var box = byId("pickList");
    if (!state.pickLoaded) {
      box.innerHTML = '<div class="empty">正在加载群列表…</div>';
      return;
    }
    var kw = (byId("pickSearch").value || "").trim().toLowerCase();
    var rows = state.pickGroups.filter(function (g) {
      if (!kw) return true;
      return (String(g.label) + " " + (g.group_name || "") + " " + (g.group_id || ""))
        .toLowerCase().indexOf(kw) >= 0;
    });
    if (!rows.length) {
      box.innerHTML = '<div class="empty">没有匹配的会话。换个词，或点「刷新群列表」。</div>';
      return;
    }
    box.innerHTML = rows.map(function (g) {
      var sub = [];
      if (g.group_id) sub.push("群号 " + g.group_id);
      sub.push(g.source === "pushed" ? "已在推送名单" : "机器人所见");
      return '<label class="pick-item">' +
        '<input type="checkbox" data-pick="' + esc(g.value) + '"' +
          (state.picked[g.value] ? " checked" : "") + ">" +
        '<span class="nm">' + esc(g.group_name || g.label) + "</span>" +
        '<span class="sub">' + esc(sub.join(" · ")) + "</span></label>";
    }).join("");
  }

  function applyPick() {
    // 把弹窗勾选结果灌回主表格：勾上的补进列表，去掉的如果是「机器人所见」就直接移除
    var tips = {};
    var verified = {};
    Object.keys(state.picked).forEach(function (umo) {
      if (state.picked[umo]) tips[umo] = true;
    });
    state.pickGroups.forEach(function (g) {
      if (state.picked[g.value]) verified[g.value] = g;
    });

    var rows = [];
    state.groups.forEach(function (g) {
      if (verified[g.umo]) { rows.push(g); tips[g.umo] = true; }
    });
    Object.keys(tips).forEach(function (umo) {
      var exists = rows.some(function (r) { return r.umo === umo; });
      if (exists) return;
      var meta = verified[umo] || {};
      rows.push({
        umo: umo,
        label: meta.label || umo,
        group_id: meta.group_id || "",
        group_name: meta.group_name || "",
        enabled: true,
        last_pushed: ""
      });
    });
    state.groups = rows;
    renderGroups();
    byId("pickMask").hidden = true;
    toast("已选 " + rows.length + " 个群，记得点「保存推送群」");
  }

  // ---------- 绑定 & 启动 ----------

  function bind() {
    byId("btnReload").addEventListener("click", function () {
      loadAll().then(function () { toast("已刷新"); }).catch(fail);
    });
    byId("btnSaveGroups").addEventListener("click", saveGroups);
    byId("btnPushAll").addEventListener("click", pushAll);
    byId("btnResetAll").addEventListener("click", function () { resetRecords(""); });
    byId("btnClearShots").addEventListener("click", clearShots);
    byId("btnClearCache").addEventListener("click", clearCache);
    byId("btnSaveConfig").addEventListener("click", saveConfig);
    byId("btnPickGroup").addEventListener("click", openPick);
    byId("pickClose").addEventListener("click", function () { byId("pickMask").hidden = true; });
    byId("btnPickApply").addEventListener("click", applyPick);
    byId("pickSearch").addEventListener("input", renderPickList);
    byId("btnPickRefresh").addEventListener("click", function () {
      loadPickGroups(true).catch(fail);
    });
    byId("pickList").addEventListener("change", function (e) {
      var box = e.target;
      if (!box || box.type !== "checkbox") return;
      var umo = box.getAttribute("data-pick");
      if (!umo) return;
      state.picked[umo] = box.checked;
      byId("pickHint").textContent = "共 " + state.pickGroups.length +
        " 个会话，当前选中 " + countPicked() + " 个";
    });

    byId("groupBody").addEventListener("click", function (e) {
      var el = e.target;
      if (!el || el.tagName !== "BUTTON") return;
      var act = el.getAttribute("data-act");
      var umo = el.getAttribute("data-umo");
      if (act === "test") openTest(umo);
      else if (act === "reset") resetRecords(umo);
    });

    byId("recordList").addEventListener("click", function (e) {
      var el = e.target;
      if (!el || el.tagName !== "BUTTON" || el.getAttribute("data-act") !== "reset-one") return;
      resetRecords(el.getAttribute("data-umo"));
    });

    byId("testClose").addEventListener("click", function () { byId("testMask").hidden = true; });
    byId("btnTestRun").addEventListener("click", runTest);

    ["pickMask", "testMask"].forEach(function (id) {
      byId(id).addEventListener("click", function (e) {
        if (e.target === this) this.hidden = true;
      });
    });
    document.addEventListener("keydown", function (e) {
      if (e.key !== "Escape") return;
      ["pickMask", "testMask"].forEach(function (id) { byId(id).hidden = true; });
    });
  }

  initTheme();
  bind();
  byId("groupBody").innerHTML = '<tr><td colspan="5" class="empty">正在连接面板…</td></tr>';
  waitForBridge()
    .then(loadAll)
    .catch(function (err) {
      byId("groupBody").innerHTML = '<tr><td colspan="5" class="empty">' +
        esc((err && err.message) || "加载失败") + "</td></tr>";
      fail(err);
    });
})();
