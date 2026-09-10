/* ExaMLOps "Platform in motion" — transit-map animation engine for the docs site.
 *
 * A page declares a player:
 *
 *   <div class="xm-player" data-scene="prediction">
 *     <ol class="xm-steps">
 *       <li data-focus="client,bus" data-run="client-bus"><strong>Title.</strong> Narration…</li>
 *     </ol>
 *   </div>
 *
 * The scene (stations + routes) is registered by a scene file with XM.register(id, scene).
 * The <ol> is the narration: it is plain HTML in the page, so it is searchable, printable and
 * readable with JavaScript off. The engine only brings it to life:
 *
 *   data-focus  station ids to highlight (comma separated)
 *   data-run    route ids to run a train along: "a,b" run together, "a;b" run one after the
 *               other, "~a" runs route a backwards. Routes listed here are highlighted too.
 *   data-actor  optional: who acts in this step (shown as a badge); data-line colours it.
 *
 * No dependencies. Respects prefers-reduced-motion (no autoplay, no moving trains), pauses when
 * scrolled out of view or when the tab is hidden, and re-mounts on Material instant navigation.
 */
(function () {
  "use strict";

  var NS = "http://www.w3.org/2000/svg";
  var XM = (window.XM = window.XM || {});
  XM.scenes = XM.scenes || {};
  XM.players = XM.players || [];
  XM.register = function (id, scene) { XM.scenes[id] = scene; };

  // assets/explore/js/xm-flow.js -> assets/explore/ and the site root, whatever the page depth.
  var me = document.currentScript && document.currentScript.src;
  XM.assetBase = me ? me.replace(/js\/xm-flow\.js(\?.*)?$/, "") : "/assets/explore/";
  XM.siteRoot = XM.assetBase.replace(/assets\/explore\/$/, "");

  var reduceMotion = window.matchMedia ? window.matchMedia("(prefers-reduced-motion: reduce)") : { matches: false };

  XM.LINES = {
    data: { label: "Data line", sub: "a prediction", color: "var(--xm-data)" },
    control: { label: "Control line", sub: "a retrain", color: "var(--xm-control)" },
    human: { label: "Decision line", sub: "a person decides", color: "var(--xm-human)" },
    hpc: { label: "Compute line", sub: "a cluster job", color: "var(--xm-hpc)" },
    observe: { label: "Signal line", sub: "metrics, logs, audit", color: "var(--xm-observe)" },
    planned: { label: "Under construction", sub: "not built yet", color: "var(--xm-planned)" }
  };

  var uidCounter = 0;

  function svg(tag, attrs, parent) {
    var n = document.createElementNS(NS, tag);
    if (attrs) for (var k in attrs) if (attrs[k] !== undefined && attrs[k] !== null) n.setAttribute(k, attrs[k]);
    if (parent) parent.appendChild(n);
    return n;
  }

  function h(tag, attrs, parent, text) {
    var n = document.createElement(tag);
    if (attrs) for (var k in attrs) {
      if (k === "class") n.className = attrs[k];
      else if (attrs[k] !== undefined && attrs[k] !== null) n.setAttribute(k, attrs[k]);
    }
    if (text !== undefined) n.textContent = text;
    if (parent) parent.appendChild(n);
    return n;
  }

  function lineColor(line) { return (XM.LINES[line] || XM.LINES.data).color; }

  function href(link) {
    if (!link) return "#";
    if (/^(https?:)?\/\//.test(link) || link.charAt(0) === "#") return link;
    return XM.siteRoot + link.replace(/^\//, "").replace(/\.md(#|$)/, "/$1").replace(/index\/$/, "");
  }

  // ------------------------------------------------------------------ geometry

  function nodeSize(n) {
    if (n.kind === "person") return { w: 52, h: 52, r: 26 };
    if (n.kind === "gate") return { w: 58, h: 58 };
    var lines = String(n.label || "").split("\n").length;
    var subs = n.sub ? String(n.sub).split("\n").length : 0;
    // Wide enough for its longest label or subtitle, so no station text can overflow its box.
    var longest = 0, longestSub = 0;
    String(n.label || "").split("\n").forEach(function (l) { longest = Math.max(longest, l.length); });
    String(n.sub || "").split("\n").forEach(function (l) { longestSub = Math.max(longestSub, l.length); });
    var auto = Math.max(150, Math.ceil(longest * 7.6 + 24), Math.ceil(longestSub * 5.8 + 20));
    return { w: n.w || auto, h: n.h || ((n.kind === "store" ? 28 : 20) + lines * 17 + subs * 14) };
  }

  function boundary(n, toward) {
    var s = nodeSize(n);
    var dx = toward.x - n.x, dy = toward.y - n.y;
    if (dx === 0 && dy === 0) return { x: n.x, y: n.y };
    if (n.kind === "person") {
      var len = Math.sqrt(dx * dx + dy * dy);
      return { x: n.x + (dx / len) * (s.r + 2), y: n.y + (dy / len) * (s.r + 2) };
    }
    if (n.kind === "gate") {
      var a = s.w / 2 + 2, t = 1 / (Math.abs(dx) / a + Math.abs(dy) / a);
      return { x: n.x + dx * t, y: n.y + dy * t };
    }
    var hw = s.w / 2 + 2, hh = s.h / 2 + 2;
    var tx = dx !== 0 ? hw / Math.abs(dx) : Infinity;
    var ty = dy !== 0 ? hh / Math.abs(dy) : Infinity;
    var k = Math.min(tx, ty);
    return { x: n.x + dx * k, y: n.y + dy * k };
  }

  function pt(p) { return { x: p[0] !== undefined ? p[0] : p.x, y: p[1] !== undefined ? p[1] : p.y }; }

  // start/end, when given, pin the exact attachment points (for clean orthogonal arrivals).
  function routePath(from, to, via, curve, start, end) {
    via = (via || []).map(pt);
    var first = via.length ? via[0] : (end ? pt(end) : to), last = via.length ? via[via.length - 1] : (start ? pt(start) : from);
    var a = start ? pt(start) : boundary(from, first), b = end ? pt(end) : boundary(to, last);
    if (curve && !via.length) {
      var mx = (a.x + b.x) / 2, my = (a.y + b.y) / 2;
      var nx = -(b.y - a.y), ny = b.x - a.x, l = Math.sqrt(nx * nx + ny * ny) || 1;
      var c = { x: mx + (nx / l) * curve, y: my + (ny / l) * curve };
      return "M" + a.x + "," + a.y + " Q" + c.x + "," + c.y + " " + b.x + "," + b.y;
    }
    var pts = [a].concat(via, [b]);
    var d = "M" + pts[0].x.toFixed(1) + "," + pts[0].y.toFixed(1);
    for (var i = 1; i < pts.length - 1; i++) {
      var p0 = pts[i - 1], p1 = pts[i], p2 = pts[i + 1];
      var d1 = Math.hypot(p1.x - p0.x, p1.y - p0.y), d2 = Math.hypot(p2.x - p1.x, p2.y - p1.y);
      var r = Math.min(16, d1 / 2, d2 / 2);
      var s = { x: p1.x - ((p1.x - p0.x) / d1) * r, y: p1.y - ((p1.y - p0.y) / d1) * r };
      var e = { x: p1.x + ((p2.x - p1.x) / d2) * r, y: p1.y + ((p2.y - p1.y) / d2) * r };
      d += " L" + s.x.toFixed(1) + "," + s.y.toFixed(1) + " Q" + p1.x + "," + p1.y + " " + e.x.toFixed(1) + "," + e.y.toFixed(1);
    }
    d += " L" + pts[pts.length - 1].x.toFixed(1) + "," + pts[pts.length - 1].y.toFixed(1);
    return d;
  }

  // ------------------------------------------------------------------ player

  function Player(root) {
    this.root = root;
    this.scene = XM.scenes[root.getAttribute("data-scene")];
    this.uid = "xm" + ++uidCounter;
    this.mode = root.getAttribute("data-mode") || "steps";
    this.idx = -1;
    this.playing = false;
    this.speed = 1;
    this.visible = false;
    this.tokens = [];
    this.timers = [];
    this.hiddenLines = {};
    this.nodeEls = {};
    this.edgeEls = {};
    if (!this.scene) {
      // Mark it mounted anyway, so a later mount pass does not add a second notice.
      root.classList.add("is-mounted");
      var note = h("p", { class: "xm-atlas-empty" }, null, "This animation could not load its map. The steps below describe it in full.");
      root.insertBefore(note, root.firstChild);
      return;
    }
    this.build();
  }

  Player.prototype.build = function () {
    var self = this, sc = this.scene, root = this.root;
    root.classList.add("is-mounted");
    var list = root.querySelector("ol.xm-steps");
    this.steps = list ? Array.prototype.map.call(list.children, function (li, i) {
      var strong = li.querySelector("strong");
      return {
        li: li,
        title: strong ? strong.textContent.replace(/[.:]\s*$/, "") : "Step " + (i + 1),
        text: li.textContent.replace(strong ? strong.textContent : "", "").trim(),
        focus: (li.getAttribute("data-focus") || "").split(",").map(trim).filter(Boolean),
        run: li.getAttribute("data-run") || "",
        lines: (li.getAttribute("data-lines") || "").split(",").map(trim).filter(Boolean),
        actor: li.getAttribute("data-actor"),
        line: li.getAttribute("data-line")
      };
    }) : (sc.steps || []).map(function (s) {
      return { title: s.title, text: s.text, focus: s.focus || [], run: s.run || "", lines: s.lines || [], actor: s.actor, line: s.line };
    });

    var wrap = h("div", { class: "xm-stage-wrap" });
    root.insertBefore(wrap, root.firstChild);
    var stage = h("div", { class: "xm-stage" }, wrap);
    h("div", { class: "xm-swipe", "aria-hidden": "true" }, wrap, "Swipe sideways to see the whole map.");
    var vb = sc.viewBox || [1200, 640];
    if (vb.length === 2) vb = [0, 0, vb[0], vb[1]];
    var s = (this.svg = svg("svg", {
      viewBox: vb.join(" "),
      role: "img",
      "aria-labelledby": this.uid + "-t " + this.uid + "-d",
      preserveAspectRatio: "xMidYMid meet"
    }, stage));
    svg("title", { id: this.uid + "-t" }, s).textContent = sc.title || "ExaMLOps flow";
    svg("desc", { id: this.uid + "-d" }, s).textContent = sc.description ||
      "Animated map. The numbered steps below the map describe each stage in text.";

    var defs = svg("defs", null, s);
    Object.keys(XM.LINES).forEach(function (line) {
      var m = svg("marker", {
        id: self.uid + "-arrow-" + line, viewBox: "0 0 10 10", refX: 7.5, refY: 5,
        markerWidth: 5.5, markerHeight: 5.5, orient: "auto-start-reverse"
      }, defs);
      svg("path", { d: "M0,0 L10,5 L0,10 z", fill: lineColor(line) }, m);
    });

    // Optional zone backdrops ("planes"), drawn first.
    var zones = svg("g", { class: "xm-zones" }, s);
    (sc.zones || []).forEach(function (z) {
      svg("rect", {
        x: z.x, y: z.y, width: z.w, height: z.h, rx: 16,
        fill: "color-mix(in srgb, " + lineColor(z.line || "data") + " 6%, transparent)",
        stroke: "color-mix(in srgb, " + lineColor(z.line || "data") + " 28%, transparent)",
        "stroke-width": 1.2, "stroke-dasharray": z.planned ? "4 6" : null
      }, zones);
      var t = svg("text", { x: z.x + 14, y: z.labelPos === "bottom" ? z.y + z.h - 12 : z.y + 22, class: "xm-edge-label", style: "font-size:13px" }, zones);
      t.textContent = z.label;
    });

    var nodesById = {};
    (sc.nodes || []).forEach(function (n) { nodesById[n.id] = n; });
    this.nodesById = nodesById;

    var gEdges = svg("g", { class: "xm-edges" }, s);
    (sc.edges || []).forEach(function (e) {
      var a = nodesById[e.from], b = nodesById[e.to];
      if (!a || !b) { if (window.console) console.warn("xm: route", e.id, "references a missing station"); return; }
      var line = e.line || "data";
      var g = svg("g", {
        class: "xm-edge" + (e.planned || line === "planned" ? " is-planned" : "") + (e.dashed ? " is-dashed" : ""),
        "data-line": line, "data-id": e.id
      }, gEdges);
      var d = routePath(a, b, e.via, e.curve, e.start, e.end);
      svg("path", { d: d, class: "xm-edge-casing", "stroke-width": 9 }, g);
      var p = svg("path", {
        d: d, class: "xm-edge-line", stroke: lineColor(line), "stroke-width": e.weight || 4.5,
        "marker-end": e.arrow === false ? null : "url(#" + self.uid + "-arrow-" + line + ")",
        "marker-start": e.both ? "url(#" + self.uid + "-arrow-" + line + ")" : null
      }, g);
      if (e.label) {
        var len = p.getTotalLength ? safeLen(p) : 0;
        var mid = len ? p.getPointAtLength(len * (e.labelAt || 0.5)) : { x: (a.x + b.x) / 2, y: (a.y + b.y) / 2 };
        var tl = svg("text", {
          x: mid.x + (e.labelDx || 0), y: mid.y + (e.labelDy !== undefined ? e.labelDy : -9),
          "text-anchor": "middle", class: "xm-edge-label"
        }, g);
        tl.textContent = e.label;
      }
      self.edgeEls[e.id] = { g: g, path: p, line: line, def: e };
    });

    var gNodes = svg("g", { class: "xm-nodes" }, s);
    (sc.nodes || []).forEach(function (n) { self.drawNode(gNodes, n); });

    this.tokenLayer = svg("g", { class: "xm-tokens" }, s);

    // Station panel.
    var panel = (this.panel = h("div", { class: "xm-panel-card", hidden: "", role: "dialog", "aria-modal": "false" }, wrap));
    var close = h("button", { class: "xm-panel-close", type: "button", "aria-label": "Close station details" }, panel, "×");
    close.addEventListener("click", function () { self.closePanel(); });
    this.panelBody = h("div", null, panel);

    // Legend.
    if (sc.legend !== false) {
      var used = {};
      (sc.edges || []).forEach(function (e) { used[e.line || "data"] = true; });
      var legend = h("div", { class: "xm-legend", role: "group", "aria-label": "Show or hide lines" }, wrap);
      Object.keys(XM.LINES).forEach(function (line) {
        if (!used[line]) return;
        var L = XM.LINES[line];
        var chip = h("button", {
          type: "button", class: "xm-chip" + (line === "planned" ? " is-planned" : ""),
          "aria-pressed": "false", style: "--chip:" + L.color,
          title: "Show only this line (click again to show all)"
        }, legend, L.label);
        chip.addEventListener("click", function () { self.isolate(line, chip, legend); });
      });
    }

    if (this.mode === "ambient") {
      this.buildCaption(wrap);
      this.setupVisibility();
      return;
    }

    if (!this.steps.length) return;

    // Controls.
    var ctl = h("div", { class: "xm-controls" }, wrap);
    this.btnPrev = this.button(ctl, "Previous step", "M15.4 7.4 14 6l-6 6 6 6 1.4-1.4L10.8 12z");
    this.btnPlay = this.button(ctl, "Play", "M8 5v14l11-7z", true);
    this.btnNext = this.button(ctl, "Next step", "M8.6 16.6 10 18l6-6-6-6-1.4 1.4 4.6 4.6z");
    this.btnRestart = this.button(ctl, "Restart", "M12 5V1L7 6l5 5V7a5 5 0 1 1-5 5H5a7 7 0 1 0 7-7z");
    var prog = h("div", { class: "xm-progress", role: "presentation" }, ctl);
    this.progress = h("span", null, prog);
    this.counter = h("span", { class: "xm-counter", "aria-hidden": "true" }, ctl);
    var speed = (this.speedSel = h("select", { class: "xm-speed", "aria-label": "Animation speed" }, ctl));
    [["0.5", "0.5×"], ["1", "1×"], ["2", "2×"]].forEach(function (o) {
      var opt = h("option", { value: o[0] }, speed, o[1]);
      if (o[0] === "1") opt.selected = true;
    });
    speed.addEventListener("change", function () { self.speed = parseFloat(speed.value) || 1; });
    this.btnPrev.addEventListener("click", function () { self.pause(); self.go(self.idx - 1); });
    this.btnNext.addEventListener("click", function () { self.pause(); self.go(self.idx + 1); });
    this.btnRestart.addEventListener("click", function () { self.go(0); if (!reduceMotion.matches) self.play(); });
    this.btnPlay.addEventListener("click", function () { self.playing ? self.pause() : self.play(); });

    this.buildCaption(wrap);

    this.steps.forEach(function (st, i) {
      if (!st.li) return;
      st.li.tabIndex = 0;
      st.li.setAttribute("role", "button");
      if (st.line) st.li.style.setProperty("--step-color", lineColor(st.line));
      st.li.addEventListener("click", function (ev) {
        if (ev.target.closest && ev.target.closest("a")) return;
        self.pause(); self.go(i);
        self.svg.scrollIntoView && self.root.getBoundingClientRect().top < 0 && self.root.scrollIntoView({ behavior: reduceMotion.matches ? "auto" : "smooth", block: "start" });
      });
      st.li.addEventListener("keydown", function (ev) {
        if (ev.key === "Enter" || ev.key === " ") { ev.preventDefault(); self.pause(); self.go(i); }
      });
    });

    root.addEventListener("keydown", function (ev) {
      if (ev.key === "Escape") self.closePanel();
    });

    this.go(0, true);
    this.setupVisibility();
  };

  Player.prototype.buildCaption = function (wrap) {
    var cap = (this.caption = h("div", { class: "xm-caption", "aria-live": "polite" }, wrap));
    this.capTitle = h("h4", null, cap);
    this.capText = h("p", null, cap);
    if (this.mode === "ambient" && this.scene.intro) {
      this.capTitle.textContent = this.scene.intro.title;
      this.capText.textContent = this.scene.intro.text;
    }
  };

  Player.prototype.button = function (parent, label, path, primary) {
    var b = h("button", { type: "button", class: "xm-btn" + (primary ? " is-primary" : ""), "aria-label": label, title: label }, parent);
    var ic = svg("svg", { viewBox: "0 0 24 24", "aria-hidden": "true" }, b);
    svg("path", { d: path }, ic);
    b._icon = ic.firstChild;
    return b;
  };

  Player.prototype.drawNode = function (parent, n) {
    var self = this, s = nodeSize(n), line = n.line;
    var color = line ? lineColor(line) : "var(--xm-node-stroke)";
    var g = svg("g", {
      class: "xm-node kind-" + (n.kind || "service"), transform: "translate(" + n.x + "," + n.y + ")",
      tabindex: n.info || n.sub ? "0" : "-1", role: "button", "data-id": n.id, "data-line": line || null,
      "aria-label": (n.label || "").replace(/\n/g, " ") + (n.sub ? ", " + String(n.sub).replace(/\n/g, " ") : "") + ". Open details."
    }, parent);
    svg("title", null, g).textContent = (n.label || "").replace(/\n/g, " ");
    var labelLines = String(n.label || "").split("\n");
    var subLines = n.sub ? String(n.sub).split("\n") : [];

    if (n.kind === "person") {
      svg("circle", { r: s.r + 7, class: "xm-pulse", stroke: color }, g);
      svg("circle", { r: s.r + 5, class: "xm-focus-ring" }, g);
      svg("circle", { r: s.r, class: "xm-node-body", style: "stroke:" + color }, g);
      svg("circle", { cx: 0, cy: -7, r: 7, fill: color }, g);
      svg("path", { d: "M-13,15 C-13,4 13,4 13,15 Z", fill: color }, g);
      this.labelAt(g, n, labelLines, subLines, s.r);
    } else if (n.kind === "gate") {
      var a = s.w / 2;
      var dpath = "M0," + -a + " L" + a + ",0 L0," + a + " L" + -a + ",0 Z";
      svg("path", { d: dpath, class: "xm-pulse", stroke: color, transform: "scale(1.12)" }, g);
      svg("path", { d: "M0," + -(a + 5) + " L" + (a + 5) + ",0 L0," + (a + 5) + " L" + -(a + 5) + ",0 Z", class: "xm-focus-ring" }, g);
      svg("path", { d: dpath, class: "xm-node-body", style: "stroke:" + color }, g);
      var icon = svg("text", { y: 6, "text-anchor": "middle", class: "xm-node-label", style: "font-size:18px;fill:" + color }, g);
      icon.textContent = n.icon || "?";
      this.labelAt(g, n, labelLines, subLines, a);
    } else {
      var x = -s.w / 2, y = -s.h / 2;
      svg("rect", { x: x - 5, y: y - 5, width: s.w + 10, height: s.h + 10, rx: 14, class: "xm-focus-ring" }, g);
      svg("rect", { x: x, y: y, width: s.w, height: s.h, rx: 10, class: "xm-pulse", stroke: color }, g);
      if (n.kind === "store") {
        var ry = 7;
        svg("path", {
          d: "M" + x + "," + (y + ry) + " a" + s.w / 2 + "," + ry + " 0 0,0 " + s.w + ",0 v" + (s.h - 2 * ry) +
             " a" + s.w / 2 + "," + ry + " 0 0,1 " + -s.w + ",0 z",
          class: "xm-node-body", style: line ? "stroke:" + color : null
        }, g);
        svg("ellipse", { cx: 0, cy: y + ry, rx: s.w / 2, ry: ry, class: "xm-node-body", style: line ? "stroke:" + color : null }, g);
      } else {
        svg("rect", { x: x, y: y, width: s.w, height: s.h, rx: 10, class: "xm-node-body", style: line ? "stroke:" + color : null }, g);
        if (line && n.kind !== "external" && n.kind !== "planned") {
          svg("path", {
            d: "M" + (x + 10) + "," + y + " h" + (s.w - 20) + " a10,10 0 0,1 10,10 v-4 h-" + s.w + " v4 a10,10 0 0,1 10,-10 z",
            fill: color, class: "xm-node-bar"
          }, g);
        }
      }
      var total = labelLines.length * 17 + subLines.length * 14;
      var ty = -total / 2 + 13 + (n.kind === "store" ? 7 : 2);
      labelLines.forEach(function (t, i) {
        var tx = svg("text", { x: 0, y: ty + i * 17, "text-anchor": "middle", class: "xm-node-label" }, g);
        tx.textContent = t;
      });
      subLines.forEach(function (t, i) {
        var tx = svg("text", { x: 0, y: ty + labelLines.length * 17 + i * 14 - 2, "text-anchor": "middle", class: "xm-node-sub" }, g);
        tx.textContent = t;
      });
      if (n.kind === "planned") {
        var tag = svg("text", { x: s.w / 2 - 4, y: y - 5, "text-anchor": "end", class: "xm-node-sub", style: "font-style:italic" }, g);
        tag.textContent = "planned";
      }
    }

    function open(ev) { if (ev) ev.stopPropagation(); self.openPanel(n); }
    g.addEventListener("click", open);
    g.addEventListener("keydown", function (ev) {
      if (ev.key === "Enter" || ev.key === " ") { ev.preventDefault(); open(); }
    });
    this.nodeEls[n.id] = g;
  };

  // Label placement for round/diamond stations: below (default), right, left or belowRight,
  // so a label can be moved off whichever side a route enters from.
  Player.prototype.labelAt = function (g, n, labelLines, subLines, radius) {
    var side = n.labelSide || "below";
    var x = 0, y0 = radius + 17, anchor = "middle";
    if (side === "right") { x = radius + 10; y0 = 5 - (labelLines.length + subLines.length - 1) * 7; anchor = "start"; }
    if (side === "left") { x = -radius - 10; y0 = 5 - (labelLines.length + subLines.length - 1) * 7; anchor = "end"; }
    if (side === "belowRight") { x = radius * 0.45; y0 = radius + 15; anchor = "start"; }
    if (side === "above") { y0 = -radius - 12 - (labelLines.length - 1) * 16 - subLines.length * 13; }
    labelLines.forEach(function (t, i) {
      var tx = svg("text", { x: x, y: y0 + i * 16, "text-anchor": anchor, class: "xm-node-label" }, g);
      tx.textContent = t;
    });
    subLines.forEach(function (t, i) {
      var tx = svg("text", { x: x, y: y0 + labelLines.length * 16 + i * 13 - 1, "text-anchor": anchor, class: "xm-node-sub" }, g);
      tx.textContent = t;
    });
  };

  // ------------------------------------------------------------------ station panel

  Player.prototype.openPanel = function (n) {
    var info = n.info || {};
    var body = this.panelBody;
    body.innerHTML = "";
    this.panel.style.setProperty("--panel-color", n.line ? lineColor(n.line) : "var(--md-accent-fg-color)");
    h("h4", null, body, info.title || String(n.label || "").replace(/\n/g, " "));
    var sub = info.sub || (n.sub ? String(n.sub).replace(/\n/g, " ") : "");
    if (sub) h("p", { class: "xm-panel-sub" }, body, sub);
    if (info.tasks && info.tasks.length) {
      h("strong", null, body, info.tasksTitle || "What it does");
      var ul = h("ul", null, body);
      info.tasks.forEach(function (t) { h("li", null, ul, t); });
    }
    if (info.human && info.human.length) {
      h("strong", null, body, "What a person does here");
      var ul2 = h("ul", null, body);
      info.human.forEach(function (t) { h("li", null, ul2, t); });
    }
    if (info.cli && info.cli.length) {
      h("strong", null, body, "Try it");
      var ul3 = h("ul", null, body);
      info.cli.forEach(function (c) { var li = h("li", null, ul3); h("code", null, li, c); });
    }
    if (info.links && info.links.length) {
      var p = h("p", { class: "xm-panel-meta" }, body);
      info.links.forEach(function (l, i) {
        if (i) p.appendChild(document.createTextNode("  ·  "));
        h("a", { href: href(l.href) }, p, l.text);
      });
    }
    if (info.meta) h("p", { class: "xm-panel-meta" }, body, info.meta);
    this.panel.hidden = false;
    this.panelNode = n.id;
    var close = this.panel.querySelector(".xm-panel-close");
    if (close) close.focus({ preventScroll: true });
  };

  Player.prototype.closePanel = function () {
    if (this.panel.hidden) return;
    this.panel.hidden = true;
    var el = this.nodeEls[this.panelNode];
    if (el) el.focus({ preventScroll: true });
  };

  // ------------------------------------------------------------------ lines

  Player.prototype.isolate = function (line, chip, legend) {
    var on = chip.getAttribute("aria-pressed") !== "true";
    Array.prototype.forEach.call(legend.children, function (c) { c.setAttribute("aria-pressed", "false"); });
    chip.setAttribute("aria-pressed", on ? "true" : "false");
    var all = this.svg.querySelectorAll("[data-line]");
    Array.prototype.forEach.call(all, function (el) {
      el.classList.toggle("is-hidden-line", on && el.getAttribute("data-line") !== line);
    });
    this.onlyLine = on ? line : null;
    if (this.mode === "ambient") {
      var L = XM.LINES[line];
      var story = this.scene.lineStories && this.scene.lineStories[line];
      if (on && story) { this.capTitle.textContent = L.label + ": " + story.title; this.capText.textContent = story.text; }
      else if (!on && this.scene.intro) { this.capTitle.textContent = this.scene.intro.title; this.capText.textContent = this.scene.intro.text; }
    }
  };

  // ------------------------------------------------------------------ steps

  function trim(s) { return s.trim(); }
  function safeLen(p) { try { return p.getTotalLength(); } catch (e) { return 0; } }

  Player.prototype.clearTimers = function () {
    this.timers.forEach(clearTimeout);
    this.timers = [];
    this.tokens.forEach(function (t) { t.dead = true; if (t.g.parentNode) t.g.parentNode.removeChild(t.g); });
    this.tokens = [];
  };

  Player.prototype.go = function (i, silent) {
    var n = this.steps.length;
    if (!n) return;
    i = ((i % n) + n) % n;
    this.clearTimers();
    this.idx = i;
    var st = this.steps[i], self = this;

    var groups = st.run ? st.run.split(";").map(function (g) { return g.split(",").map(trim).filter(Boolean); }) : [];
    var activeEdges = {};
    groups.forEach(function (g) { g.forEach(function (id) { activeEdges[id.replace(/^~/, "")] = true; }); });
    var activeNodes = {};
    st.focus.forEach(function (id) { activeNodes[id] = true; });
    Object.keys(activeEdges).forEach(function (id) {
      var e = self.edgeEls[id];
      if (e && !st.focus.length) { activeNodes[e.def.from] = true; activeNodes[e.def.to] = true; }
    });

    var focusing = st.focus.length || Object.keys(activeEdges).length;
    this.root.classList.toggle("is-focusing", !!focusing);
    Object.keys(this.nodeEls).forEach(function (id) { self.nodeEls[id].classList.toggle("is-active", !!activeNodes[id]); });
    Object.keys(this.edgeEls).forEach(function (id) { self.edgeEls[id].g.classList.toggle("is-active", !!activeEdges[id]); });

    this.steps.forEach(function (s, k) {
      if (!s.li) return;
      s.li.classList.toggle("is-current", k === i);
      if (k === i) s.li.setAttribute("aria-current", "step"); else s.li.removeAttribute("aria-current");
    });
    if (this.progress) this.progress.style.width = ((i + 1) / n) * 100 + "%";
    if (this.counter) this.counter.textContent = (i + 1) + " / " + n;

    this.capTitle.innerHTML = "";
    if (st.actor) {
      var badge = h("span", { class: "xm-actor", style: "--actor:" + lineColor(st.line || "human") }, this.capTitle, st.actor);
      badge.setAttribute("aria-label", "Actor: " + st.actor);
    }
    this.capTitle.appendChild(document.createTextNode(st.title));
    this.capText.textContent = st.text;

    if (silent) return;
    var t = 0;
    var moving = !reduceMotion.matches;
    groups.forEach(function (g) {
      var longest = 0;
      g.forEach(function (id) {
        var rev = id.charAt(0) === "~";
        var e = self.edgeEls[id.replace(/^~/, "")];
        if (!e) return;
        var dur = Math.max(650, safeLen(e.path) / 0.32) / self.speed;
        longest = Math.max(longest, dur);
        if (moving) self.timers.push(setTimeout(function () { self.runToken(e, dur, rev); }, t));
      });
      t += moving ? longest + 120 / self.speed : 0;
    });
    var dwell = (moving ? 1700 : 4200) / this.speed;
    if (this.playing) {
      this.timers.push(setTimeout(function () {
        if (!self.playing) return;
        if (self.idx === n - 1) {
          self.timers.push(setTimeout(function () { if (self.playing) self.go(0); }, 1200 / self.speed));
        } else self.go(self.idx + 1);
      }, t + dwell));
    }
  };

  Player.prototype.runToken = function (e, dur, rev) {
    var self = this, len = safeLen(e.path);
    if (!len) return;
    var g = svg("g", { class: "xm-token", "data-line": e.line }, this.tokenLayer);
    svg("rect", { x: -12, y: -6, width: 24, height: 12, rx: 6, fill: lineColor(e.line), stroke: "var(--xm-ground)", "stroke-width": 2.5 }, g);
    svg("rect", { x: -6, y: -2, width: 3, height: 4, rx: 1, fill: "var(--xm-ground)", opacity: 0.85 }, g);
    svg("rect", { x: 1, y: -2, width: 3, height: 4, rx: 1, fill: "var(--xm-ground)", opacity: 0.85 }, g);
    if (this.onlyLine && this.onlyLine !== e.line) g.classList.add("is-hidden-line");
    var tok = { g: g, dead: false };
    this.tokens.push(tok);
    var start = null;
    function frame(ts) {
      if (tok.dead) return;
      if (start === null) start = ts;
      var k = Math.min(1, (ts - start) / dur);
      var ease = k < 0.5 ? 2 * k * k : 1 - Math.pow(-2 * k + 2, 2) / 2;
      var at = (rev ? 1 - ease : ease) * len;
      var p = e.path.getPointAtLength(at);
      var q = e.path.getPointAtLength(Math.min(len, Math.max(0, at + (rev ? -1.5 : 1.5))));
      var ang = Math.atan2(q.y - p.y, q.x - p.x) * 180 / Math.PI;
      g.setAttribute("transform", "translate(" + p.x.toFixed(1) + "," + p.y.toFixed(1) + ") rotate(" + ang.toFixed(1) + ")");
      if (k < 1) requestAnimationFrame(frame);
      else {
        g.style.transition = "opacity .5s ease";
        g.style.opacity = "0";
        self.timers.push(setTimeout(function () { tok.dead = true; if (g.parentNode) g.parentNode.removeChild(g); }, 520));
      }
    }
    requestAnimationFrame(frame);
  };

  Player.prototype.play = function () {
    if (this.mode === "ambient") { this.startAmbient(); return; }
    this.playing = true;
    this.setPlayIcon();
    this.go(this.idx < 0 ? 0 : this.idx);
  };

  Player.prototype.pause = function () {
    this.playing = false;
    this.setPlayIcon();
    this.timers.forEach(clearTimeout);
    this.timers = [];
  };

  Player.prototype.setPlayIcon = function () {
    if (!this.btnPlay) return;
    this.btnPlay._icon.setAttribute("d", this.playing ? "M6 5h4v14H6zm8 0h4v14h-4z" : "M8 5v14l11-7z");
    this.btnPlay.setAttribute("aria-label", this.playing ? "Pause" : "Play");
    this.btnPlay.title = this.playing ? "Pause" : "Play";
  };

  // ------------------------------------------------------------------ ambient (home map)

  Player.prototype.startAmbient = function () {
    if (this.ambientOn || reduceMotion.matches) return;
    this.ambientOn = true;
    var self = this;
    var edges = Object.keys(this.edgeEls).map(function (k) { return self.edgeEls[k]; })
      .filter(function (e) { return e.line !== "planned"; });
    function tick() {
      if (!self.ambientOn) return;
      var pool = self.onlyLine ? edges.filter(function (e) { return e.line === self.onlyLine; }) : edges;
      if (pool.length) {
        var e = pool[Math.floor(Math.random() * pool.length)];
        if (self.tokens.length < 14) self.runToken(e, Math.max(900, safeLen(e.path) / 0.22), false);
      }
      self.ambientTimer = setTimeout(tick, self.onlyLine ? 380 : 260);
    }
    tick();
  };

  Player.prototype.stopAmbient = function () {
    this.ambientOn = false;
    clearTimeout(this.ambientTimer);
  };

  // ------------------------------------------------------------------ lifecycle

  Player.prototype.setupVisibility = function () {
    var self = this;
    var auto = this.root.getAttribute("data-autoplay") !== "false" && !reduceMotion.matches;
    if (!("IntersectionObserver" in window)) { if (auto) this.play(); return; }
    this.io = new IntersectionObserver(function (entries) {
      entries.forEach(function (en) {
        self.visible = en.isIntersecting;
        if (en.isIntersecting && auto && !self.userPaused) self.play();
        else if (!en.isIntersecting) {
          if (self.mode === "ambient") self.stopAmbient();
          else if (self.playing) { self.pause(); }
        }
      });
    }, { threshold: 0.35 });
    // Watch the map, not the whole player: with its narration list the player can be several
    // screens tall on a phone, and 30% of it would never be visible at once.
    this.io.observe(this.root.querySelector(".xm-stage-wrap") || this.root);
    if (this.btnPlay) {
      this.btnPlay.addEventListener("click", function () { self.userPaused = !self.playing; });
      [this.btnPrev, this.btnNext].forEach(function (b) { b.addEventListener("click", function () { self.userPaused = true; }); });
      this.steps.forEach(function (st) { st.li && st.li.addEventListener("click", function () { self.userPaused = true; }); });
    }
  };

  Player.prototype.destroy = function () {
    this.pause();
    this.stopAmbient();
    this.clearTimers();
    if (this.io) this.io.disconnect();
  };

  document.addEventListener("visibilitychange", function () {
    XM.players.forEach(function (p) {
      if (document.hidden) { if (p.playing) { p.wasPlaying = true; p.pause(); } p.stopAmbient && p.stopAmbient(); }
      else if (p.visible && !p.userPaused && !reduceMotion.matches) { if (p.mode === "ambient" || p.wasPlaying) p.play(); p.wasPlaying = false; }
    });
  });

  XM.mountAll = function () {
    XM.players = XM.players.filter(function (p) {
      if (document.body.contains(p.root)) return true;
      p.destroy();
      return false;
    });
    var roots = document.querySelectorAll(".xm-player[data-scene]:not(.is-mounted)");
    Array.prototype.forEach.call(roots, function (r) {
      try { XM.players.push(new Player(r)); } catch (err) { if (window.console) console.error("xm:", err); }
    });
  };

  // Material's instant navigation swaps the page without a reload: re-mount on every swap.
  // mountAll is idempotent (it skips mounted players), so calling it both now and on every
  // document$ emission is safe whether or not document$ replays the initial page.
  function boot() {
    XM.mountAll();
    if (window.document$ && typeof window.document$.subscribe === "function") {
      window.document$.subscribe(function () { setTimeout(XM.mountAll, 0); });
    }
  }
  // Scene files load after this one; mount once everything on the page has run.
  if (document.readyState === "complete") setTimeout(boot, 0);
  else window.addEventListener("load", boot);
})();
