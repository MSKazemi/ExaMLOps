/* Capability atlas: every `exa` command, grouped into the twelve lifecycle panels of
 * `exa --help`. Data comes from assets/explore/data/capabilities.json, generated from the live
 * CLI by platform/ci/gen_capability_atlas.py (CI fails if it goes stale). Progressive
 * enhancement: without JavaScript the page shows its static panel summary. */
(function () {
  "use strict";
  var PALETTE = ["#0f8b8d", "#c77d00", "#3957d6", "#b8337a", "#3f8a43", "#7a4fc4",
                 "#1f7fb8", "#b85a1f", "#2f8f6f", "#9a3fa8", "#5f7383", "#a8862a"];

  function el(tag, attrs, parent, text) {
    var n = document.createElement(tag);
    if (attrs) Object.keys(attrs).forEach(function (k) {
      if (k === "class") n.className = attrs[k]; else if (attrs[k] != null) n.setAttribute(k, attrs[k]);
    });
    if (text != null) n.textContent = text;
    if (parent) parent.appendChild(n);
    return n;
  }

  function site(p) {
    var root = (window.XM && window.XM.siteRoot) || "/";
    return root + p.replace(/\.md(#|$)/, "/$1");
  }

  function mount(root) {
    if (root.getAttribute("data-mounted")) return;
    root.setAttribute("data-mounted", "1");
    var base = (window.XM && window.XM.assetBase) || "/assets/explore/";
    fetch(base + "data/capabilities.json").then(function (r) { return r.json(); }).then(function (data) {
      render(root, data);
    }).catch(function () {
      el("p", { class: "xm-atlas-empty" }, root, "The command list could not load. The full reference is in the exa CLI command guide.");
    });
  }

  function render(root, data) {
    root.innerHTML = "";
    var panels = data.panels.filter(function (p) { return p.commands.length; });
    var state = { panel: null, q: "" };

    var bar = el("div", { class: "xm-atlas-bar", "aria-hidden": "true" }, root);
    var legend = el("div", { class: "xm-legend xm-atlas-legend", role: "group", "aria-label": "Filter by lifecycle area" }, root);
    var toggles = [];
    function select(slug) {
      state.panel = state.panel === slug ? null : slug;
      toggles.forEach(function (x) { x.el.setAttribute("aria-pressed", x.slug === state.panel ? "true" : "false"); });
      draw();
    }
    panels.forEach(function (p, i) {
      p.color = PALETTE[i % PALETTE.length];
      var b = el("button", {
        type: "button", tabindex: "-1", "aria-pressed": "false",
        title: p.title + ": " + p.commands.length + " commands",
        style: "--n:" + p.commands.length + ";--seg:" + p.color + ";--i:" + i
      }, bar, p.commands.length >= 28 ? p.title.split(" ")[0].replace(",", "") : "");
      var c = el("button", { type: "button", class: "xm-chip", "aria-pressed": "false", style: "--chip:" + p.color }, legend, p.title + " " + p.commands.length);
      toggles.push({ el: b, slug: p.slug }, { el: c, slug: p.slug });
      b.addEventListener("click", function () { select(p.slug); });
      c.addEventListener("click", function () { select(p.slug); });
    });

    var tools = el("div", { class: "xm-atlas-tools" }, root);
    var label = el("label", { class: "xm-sr", for: "xm-atlas-q" }, tools, "Search commands");
    var q = el("input", { type: "search", id: "xm-atlas-q", autocomplete: "off",
      placeholder: "Search " + data.total_commands + " commands: try drift, promote, carbon, secrets" }, tools);
    var count = el("span", { class: "xm-atlas-count", "aria-live": "polite" }, tools);
    var clear = el("button", { type: "button", class: "xm-btn" }, tools, "Show all");
    clear.addEventListener("click", function () {
      state.q = ""; q.value = ""; state.panel = null;
      toggles.forEach(function (x) { x.el.setAttribute("aria-pressed", "false"); });
      draw();
    });
    var t;
    q.addEventListener("input", function () { clearTimeout(t); t = setTimeout(function () { state.q = q.value.trim().toLowerCase(); draw(); }, 90); });

    var list = el("div", null, root);

    function draw() {
      list.innerHTML = "";
      var shown = 0;
      var words = state.q ? state.q.split(/\s+/) : [];
      panels.forEach(function (p) {
        if (state.panel && state.panel !== p.slug) return;
        var cmds = p.commands.filter(function (c) {
          if (!words.length) return true;
          var hay = (c.cmd + " " + c.help + " " + p.title).toLowerCase();
          return words.every(function (w) { return hay.indexOf(w) !== -1; });
        });
        if (!cmds.length) return;
        shown += cmds.length;
        var sec = el("section", { class: "xm-atlas-panel", style: "--seg:" + p.color }, list);
        var h3 = el("h3", { id: "area-" + p.slug }, sec, p.title + " ");
        el("small", null, h3, cmds.length + (cmds.length === p.commands.length ? "" : " of " + p.commands.length) + " commands");
        el("p", null, sec, p.blurb);
        var ul = el("ul", { class: "xm-cmd-list" }, sec);
        cmds.forEach(function (c) {
          var li = el("li", null, ul);
          el("code", null, li, c.cmd);
          var help = c.help.length > 190 ? c.help.slice(0, 187).replace(/\s+\S*$/, "") + "…" : c.help;
          el("span", { class: "xm-cmd-help" }, li, help);
          var links = el("span", { class: "xm-cmd-links" }, li);
          if (c.anchor) el("a", { href: site("reference/cli-commands-guide.md") + "#" + c.anchor }, links, "Reference");
          if (c.guide) el("a", { href: site(c.guide) }, links, "Guide");
        });
      });
      if (!shown) el("p", { class: "xm-atlas-empty" }, list, "No command matches “" + state.q + "”. Try a shorter word, or clear the filter.");
      count.textContent = shown === data.total_commands ? data.total_commands + " commands in " + panels.length + " areas"
        : "Showing " + shown + " of " + data.total_commands + " commands";
    }
    draw();
  }

  function mountAll() {
    Array.prototype.forEach.call(document.querySelectorAll(".xm-atlas"), mount);
  }
  function boot() {
    mountAll();
    if (window.document$ && window.document$.subscribe) window.document$.subscribe(function () { setTimeout(mountAll, 0); });
  }
  if (document.readyState === "complete") setTimeout(boot, 0); else window.addEventListener("load", boot);
})();
