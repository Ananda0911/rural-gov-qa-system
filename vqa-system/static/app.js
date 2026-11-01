/* 全站 JS 工具：API 封装（自动带 CSRF 头）、提示、确认 */
(function () {
  function csrf() {
    var m = document.querySelector('meta[name="csrf"]');
    return m ? m.getAttribute("content") : "";
  }
  window.api = async function (url, method, body) {
    var opt = { method: method || "GET", headers: { "X-CSRF-Token": csrf() } };
    if (body !== undefined && body !== null) {
      opt.headers["Content-Type"] = "application/json";
      opt.body = JSON.stringify(body);
    }
    var resp = await fetch(url, opt);
    var data = null;
    try { data = await resp.json(); } catch (e) { /* 204 等无体响应 */ }
    if (!resp.ok) {
      var d = data && data.detail ? data.detail : (data || {});
      var err = new Error(d.message || ("请求失败（" + resp.status + "）"));
      err.code = d.code || "";
      err.status = resp.status;
      err.data = d;
      throw err;
    }
    return data;
  };
  var toastEl = null;
  window.toast = function (msg, isErr) {
    if (!toastEl) {
      toastEl = document.createElement("div");
      toastEl.id = "toast";
      document.body.appendChild(toastEl);
    }
    toastEl.textContent = msg;
    toastEl.className = "show" + (isErr ? " err" : "");
    clearTimeout(toastEl._t);
    toastEl._t = setTimeout(function () { toastEl.className = ""; }, 2600);
  };
  window.esc = function (s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  };
  window.logout = async function () {
    if (!confirm("确定退出登录？")) return;
    try { await api("/api/auth/logout", "POST", {}); } catch (e) { /* 会话失效也继续 */ }
    location.href = "/login";
  };
})();
