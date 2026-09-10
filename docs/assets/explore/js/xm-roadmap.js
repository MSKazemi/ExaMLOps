/* Roadmap timeline: one route from the first shipped milestone to the last planned item.
 * Data: assets/explore/data/roadmap.json. The solid part of the route fills up to "today"
 * when the timeline scrolls into view; everything after it is dotted track. Filters by area
 * and status. Progressive enhancement: the page's static summary stays if this cannot load. */
(function () {
  "use strict";
  var AREA = {
    data: ["Data", "var(--xm-data)"], training: ["Training", "var(--xm-control)"],
    serving: ["Serving", "#1f7fb8"], monitoring: ["Monitoring", "var(--xm-observe)"],
    governance: ["Governance", "var(--xm-human)"], hpc: ["HPC", "var(--xm-hpc)"],
    llmops: ["LLMOps", "#7a4fc4"], agents: ["Agents", "#9a3fa8"], dashboard: ["Dashboard", "#2f8f6f"],
    cli: ["CLI", "#5f7383"], platform: ["Platform", "#a8862a"]
  };
  var STATUS = { shipped: "Shipped", partial: "Partly built", planned: "Planned", "in design": "In design", deferred: "Deferred" };

  function el(tag, attrs, parent, text) {
    var n = document.createElement(tag);
    if (attrs) Object.keys(attrs).forEach(function (k) {
      if (k === "class") n.className = attrs[k]; else if (attrs[k] != null) n.setAttribute(k, attrs[k]);
    });
    if (text != null) n.textContent = text;
    if (parent) parent.appendChild(n);
    return n;
  }

  function mount(root) {
    if (root.getAttribute("data-mounted")) return;
    root.setAttribute("data-mounted", "1");
    var base = (window.XM && window.XM.assetBase) || "/assets/explore/";
    fetch(base + "data/roadmap.json").then(function (r) { return r.json(); })
      .then(function (d) { render(root, d); })
      .catch(function () { /* keep the static summary */ });
  }

  function render(root, d) {
    root.innerHTML = "";
    var state = { area: null, status: null };
    var filters = el("div", { class: "xm-road-filters", role: "group", "aria-label": "Filter the roadmap" }, root);
    var areas = {};
    d.shipped.forEach(function (e) { e.items.forEach(function (i) { areas[i.area] = 1; }); });
    d.planned.forEach(function (t) { t.items.forEach(function (i) { areas[i.area] = 1; }); });
    function chip(label, color, onClick) {
      var b = el("button", { type: "button", class: "xm-chip", "aria-pressed": "false", style: "--chip:" + color }, filters, label);
      b.addEventListener("click", function () { onClick(b); });
      return b;
    }
    var statusChips = [], areaChips = [];
    Object.keys(STATUS).forEach(function (s) {
      var c = chip(STATUS[s], s === "shipped" ? "var(--xm-control)" : "var(--xm-planned)", function (b) {
        state.status = state.status === s ? null : s;
        statusChips.forEach(function (x) { x.setAttribute("aria-pressed", "false"); });
        if (state.status) b.setAttribute("aria-pressed", "true");
        draw();
      });
      statusChips.push(c);
    });
    Object.keys(AREA).forEach(function (a) {
      if (!areas[a]) return;
      var c = chip(AREA[a][0], AREA[a][1], function (b) {
        state.area = state.area === a ? null : a;
        areaChips.forEach(function (x) { x.setAttribute("aria-pressed", "false"); });
        if (state.area) b.setAttribute("aria-pressed", "true");
        draw();
      });
      areaChips.push(c);
    });
    var count = el("p", { class: "xm-atlas-count", "aria-live": "polite" }, root);
    var road = el("div", { class: "xm-road" }, root);

    function keep(item, status) {
      return (!state.area || item.area === state.area) && (!state.status || status === state.status);
    }

    function stop(parent, item, status) {
      var a = AREA[item.area] || ["Other", "var(--xm-muted)"];
      var div = el("div", { class: "xm-stop" + (status === "shipped" ? "" : status === "partial" ? " is-partial is-planned" : " is-planned"), style: "--area:" + a[1] }, parent);
      el("strong", null, div, item.title);
      el("span", { class: "xm-tag", style: "--area:" + a[1] }, div, a[0]);
      if (status !== "shipped") el("span", { class: "xm-tag is-status" }, div, STATUS[status] || status);
      if (item.when && item.when !== "pre-v0.20") el("span", { class: "xm-stop-meta" }, div, item.when);
      el("p", null, div, item.text);
    }

    function draw() {
      road.innerHTML = "";
      var shown = 0, today = null;
      d.shipped.forEach(function (era) {
        var items = era.items.filter(function (i) { return keep(i, "shipped"); });
        if (!items.length) return;
        var sec = el("section", { class: "xm-era" }, road);
        var h = el("h3", null, sec, era.name + " ");
        el("small", { class: "xm-stop-meta" }, h, era.span + ", " + items.length + " shipped");
        items.forEach(function (i) { stop(sec, i, "shipped"); shown++; });
      });
      today = el("div", { class: "xm-era xm-today" }, road);
      var th = el("h3", null, today, "Today");
      el("small", { class: "xm-stop-meta" }, th, "everything below is under construction");
      d.planned.forEach(function (track) {
        var items = track.items.filter(function (i) { return keep(i, i.status); });
        if (!items.length) return;
        var sec = el("section", { class: "xm-era" }, road);
        var h = el("h3", null, sec, track.name + " ");
        el("small", { class: "xm-stop-meta" }, h, track.text);
        items.forEach(function (i) { stop(sec, i, i.status); shown++; });
      });
      count.textContent = "Showing " + shown + " of " + (d.counts.shipped + d.counts.planned) + " milestones";
      fill();
    }

    function fill() {
      var t = road.querySelector(".xm-today");
      if (!t) return;
      var pct = Math.max(0, Math.min(100, (t.offsetTop / Math.max(1, road.scrollHeight)) * 100));
      road.style.setProperty("--done", "0%");
      if (!("IntersectionObserver" in window)) { road.style.setProperty("--done", pct + "%"); return; }
      var io = new IntersectionObserver(function (es) {
        es.forEach(function (e) { if (e.isIntersecting) { road.style.setProperty("--done", pct + "%"); io.disconnect(); } });
      }, { threshold: 0.05 });
      io.observe(road);
    }
    draw();
  }

  function mountAll() { Array.prototype.forEach.call(document.querySelectorAll(".xm-roadmap"), mount); }
  function boot() {
    mountAll();
    if (window.document$ && window.document$.subscribe) window.document$.subscribe(function () { setTimeout(mountAll, 0); });
  }
  if (document.readyState === "complete") setTimeout(boot, 0); else window.addEventListener("load", boot);
})();
