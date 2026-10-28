/* Great Wolf Tracker dashboard behaviour: tooltips for HTML cells and the Plotly charts.
   Chart data arrives in <script type="application/json"> blocks; labels are inserted with
   textContent (never innerHTML). */
(function () {
  "use strict";

  const GWT = {};
  const css = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  GWT.data = (id) => {
    const el = document.getElementById(id);
    return el ? JSON.parse(el.textContent) : null;
  };
  const money = (v) => (v == null ? "–" : "$" + v.toLocaleString("en-CA", { minimumFractionDigits: 2, maximumFractionDigits: 2 }));
  const money0 = (v) => (v == null ? "–" : "$" + Math.round(v).toLocaleString("en-CA"));
  const ordinal = (n) => {  // pace percentiles, shown as 1st..99th
    if (n == null) return "n/a";
    const k = Math.min(99, Math.max(1, Math.round(n))), t = k % 100;
    const s = t >= 10 && t <= 20 ? "th" : ({ 1: "st", 2: "nd", 3: "rd" }[k % 10] || "th");
    return k + s;
  };
  GWT.fmt = { money, money0, ordinal };

  // ------------------------------------------------------------------ tooltip for HTML cells
  // data-tip = "Title\nvalue\tlabel\nvalue\tlabel" (values lead, labels follow)
  const tip = document.createElement("div");
  tip.className = "tip";
  tip.setAttribute("role", "tooltip");
  tip.hidden = true;
  document.addEventListener("DOMContentLoaded", () => document.body.appendChild(tip));
  let current = null;

  function renderTip(text) {
    tip.textContent = "";
    text.split("\n").forEach((line, i) => {
      const row = document.createElement("div");
      if (i === 0) {
        row.className = "t";
        row.textContent = line;
      } else {
        const parts = line.split("\t");
        row.className = "r";
        const strong = document.createElement("strong");
        strong.textContent = parts[0];
        row.appendChild(strong);
        if (parts[1]) row.appendChild(document.createTextNode(" " + parts[1]));
      }
      tip.appendChild(row);
    });
  }
  function place(x, y) {
    const pad = 14, r = tip.getBoundingClientRect();
    let left = x + pad, top = y + pad;
    if (left + r.width > window.innerWidth - 8) left = x - r.width - pad;
    if (top + r.height > window.innerHeight - 8) top = y - r.height - pad;
    tip.style.left = Math.max(8, left) + "px";
    tip.style.top = Math.max(8, top) + "px";
  }
  function show(el, x, y) {
    current = el;
    renderTip(el.getAttribute("data-tip"));
    tip.hidden = false;
    place(x, y);
  }
  function hide() {
    tip.hidden = true;
    current = null;
  }
  document.addEventListener("pointerover", (e) => {
    const el = e.target.closest && e.target.closest("[data-tip]");
    if (el && el !== current) show(el, e.clientX, e.clientY);
  });
  document.addEventListener("pointermove", (e) => {
    if (current && !tip.hidden) place(e.clientX, e.clientY);
  });
  document.addEventListener("pointerout", (e) => {
    if (!current) return;
    const to = e.relatedTarget;
    if (!to || !current.contains(to)) hide();
  });
  document.addEventListener("focusin", (e) => {
    const el = e.target.closest && e.target.closest("[data-tip]");
    if (!el) return;
    const r = el.getBoundingClientRect();
    show(el, r.right, r.bottom);
  });
  document.addEventListener("focusout", hide);
  window.addEventListener("scroll", hide, { passive: true });

  // ------------------------------------------------------------------ Plotly helpers
  function theme() {
    return {
      ink: css("--ink"), ink2: css("--ink-2"), muted: css("--muted"), grid: css("--grid"), axis: css("--axis"),
      surface: css("--surface"), weekday: css("--series-weekday"), weekend: css("--series-weekend"),
      emph: css("--emph"), old1: css("--old-1"), old2: css("--old-2"), band: css("--band"),
      bandLine: css("--band-line"), closure: css("--closure-band"), quadrant: css("--quadrant"),
      ring: css("--marker-ring"), seq2: css("--seq-2"),
    };
  }
  function axis(t, extra) {
    return Object.assign(
      { gridcolor: t.grid, linecolor: t.axis, zerolinecolor: t.axis, tickcolor: t.axis, ticks: "outside", ticklen: 4,
        automargin: true, separatethousands: true, title: { font: { size: 12, color: t.ink2 } },
        tickfont: { size: 11, color: t.ink2 } },
      extra || {}
    );
  }
  function quantile(sorted, p) {
    if (!sorted.length) return null;
    const pos = p * (sorted.length - 1), lo = Math.floor(pos), hi = Math.ceil(pos);
    return sorted[lo] + (sorted[hi] - sorted[lo]) * (pos - lo);
  }
  // Values far above the rest (e.g. a night where only cottages are left) would squash the
  // chart; above this cap they're drawn at the edge and labelled with their real value.
  function outlierCap(values) {
    const v = values.filter((x) => x != null).sort((a, b) => a - b);
    if (v.length < 8) return null;
    const cap = Math.max(quantile(v, 0.9) * 1.3, quantile(v, 0.5) * 1.8);
    return v[v.length - 1] > cap ? cap : null;
  }
  function layout(t, extra) {
    const base = {
      paper_bgcolor: "rgba(0,0,0,0)", plot_bgcolor: "rgba(0,0,0,0)",
      font: { family: 'system-ui, -apple-system, "Segoe UI", sans-serif', size: 12, color: t.ink },
      margin: { l: 58, r: 20, t: 10, b: 48 },
      hoverlabel: { bgcolor: t.surface, bordercolor: t.axis, font: { color: t.ink, size: 12 } },
      showlegend: false,
      legend: { orientation: "h", x: 0, y: 1.02, yanchor: "bottom", font: { size: 12, color: t.ink2 } },
    };
    const out = Object.assign(base, extra || {});
    out.xaxis = axis(t, extra && extra.xaxis);
    out.yaxis = axis(t, extra && extra.yaxis);
    return out;
  }
  const config = { responsive: true, displaylogo: false, displayModeBar: false };
  GWT.plot = function (el, traces, lay, onClick) {
    if (typeof el === "string") el = document.getElementById(el);
    if (!el || !window.Plotly) return null;
    window.Plotly.react(el, traces, lay, config);
    if (onClick && !el.__gwtClick) {
      el.__gwtClick = true;
      el.on("plotly_click", (ev) => {
        const p = ev.points && ev.points[0];
        const d = p && p.customdata && p.customdata[0];
        if (d) onClick(d);
      });
    }
    return el;
  };
  const goDate = (d) => { window.location.href = "/date/" + d; };

  // ------------------------------------------------------------------ booking curve
  // x = days before arrival, reversed (365 -> 0); band = median + 10th–90th pct of other dates.
  GWT.curve = function (elId, spec) {
    const t = theme();
    const pts = spec.points || [];
    const band = spec.band || [];
    const lo = [], hi = [], mid = [], bx = [], hov = [];
    // Walk buckets from far (181–365) to near (0–1) so x only ever decreases: a step band.
    band.slice().reverse().forEach((b) => {
      if (!b) {
        bx.push(null); lo.push(null); hi.push(null); mid.push(null); hov.push(null);
        return;
      }
      [b.lead_hi + 0.5, b.lead_lo - 0.5].forEach((x) => {
        bx.push(x); lo.push(b.p10); hi.push(b.p90); mid.push(b.p50);
        hov.push([b.bucket, b.n, spec.fmt(b.p10), spec.fmt(b.p50), spec.fmt(b.p90)]);
      });
    });
    const traces = [
      { x: bx, y: lo, mode: "lines", line: { width: 0, color: t.band }, hoverinfo: "skip", connectgaps: false, showlegend: false },
      { x: bx, y: hi, mode: "lines", line: { width: 0, color: t.band }, fill: "tonexty", fillcolor: t.band,
        hoverinfo: "skip", connectgaps: false, name: "Middle 80% of other dates", showlegend: true },
      { x: bx, y: mid, mode: "lines", line: { width: 1.5, color: t.bandLine }, connectgaps: false, customdata: hov,
        name: "Median of other dates", showlegend: true,
        hovertemplate: "%{customdata[0]} days out · %{customdata[1]} snapshots<br>median %{customdata[3]} · 10–90%: %{customdata[2]}–%{customdata[4]}<extra>Other dates</extra>" },
      { x: pts.map((p) => p.lead), y: pts.map((p) => p.value), mode: pts.length > 1 ? "lines+markers" : "markers",
        name: spec.name || "This date", showlegend: true,
        line: { width: 3, color: t.seq2 }, marker: { size: 9, color: t.seq2, line: { width: 2, color: t.ring } },
        customdata: pts.map((p) => [p.label, spec.fmt(p.value)]),
        hovertemplate: "%{x} days out (scraped %{customdata[0]})<br><b>%{customdata[1]}</b><extra>" + (spec.name || "This date") + "</extra>" },
    ];
    const maxLead = Math.max(30, ...pts.map((p) => p.lead), ...band.filter(Boolean).map((b) => b.lead_hi));
    const el = document.getElementById(elId);
    const narrow = el && el.clientWidth < 640;
    GWT.plot(elId, traces, layout(t, {
      showlegend: true,
      margin: { l: 62, r: 20, t: 34, b: 48 },
      hovermode: "closest",
      xaxis: { title: { text: "Days before arrival" }, range: [Math.min(365, maxLead) + 2, -2], dtick: narrow ? 90 : 30,
               tickangle: 0, zeroline: false },
      yaxis: { title: { text: spec.yTitle }, rangemode: spec.zero ? "tozero" : "normal", tickprefix: spec.prefix || "" },
    }));
  };

  // ------------------------------------------------------------------ line over arrival dates
  GWT.upcoming = function (elId, rows, key, yTitle, fmt, prefix) {
    const t = theme();
    const traces = [{
      x: rows.map((r) => r.date), y: rows.map((r) => r[key]), mode: "lines", line: { width: 2, color: t.seq2 },
      connectgaps: false, customdata: rows.map((r) => [r.date, fmt(r[key])]),
      hovertemplate: "%{x|%a %b %-d, %Y}<br><b>%{customdata[1]}</b><extra></extra>",
    }];
    GWT.plot(elId, traces, layout(t, {
      hovermode: "x",
      xaxis: { type: "date", tickformat: "%b %Y", hoverformat: "%a %b %-d" },
      yaxis: { title: { text: yTitle }, rangemode: key === "rooms_left" ? "tozero" : "normal", tickprefix: prefix || "" },
    }), goDate);
  };

  // ------------------------------------------------------------------ Plan page scatter
  function median(values) {
    const v = values.slice().sort((a, b) => a - b);
    if (!v.length) return null;
    const m = Math.floor(v.length / 2);
    return v.length % 2 ? v[m] : (v[m - 1] + v[m]) / 2;
  }

  GWT.planScatter = function (spec) {
    const state = { y: "rooms", window: 90, nights: "all" };
    const root = document.getElementById(spec.root);
    if (!root) return;
    root.querySelectorAll("[data-ctl]").forEach((btn) => {
      btn.addEventListener("click", () => {
        const ctl = btn.getAttribute("data-ctl");
        state[ctl] = ctl === "window" ? Number(btn.value) : btn.value;
        root.querySelectorAll('[data-ctl="' + ctl + '"]').forEach((b) => b.setAttribute("aria-pressed", String(b === btn)));
        draw();
      });
    });
    const tableBox = root.querySelector("details.tableview");
    if (tableBox) tableBox.addEventListener("toggle", () => tableBox.open && fillTable());

    function filtered() {
      return spec.points.filter((p) =>
        p.i < state.window && p.price != null && p[state.y] != null &&
        (state.nights === "all" || (state.nights === "weekend") === p.we));
    }
    function draw() {
      const t = theme();
      const pts = filtered();
      const yKey = state.y;
      const yTitle = yKey === "rooms" ? "Rooms left" : "Pace percentile (vs other dates at the same days-out)";
      const series = [
        { key: false, name: "Sun–Thu nights", color: t.weekday },
        { key: true, name: "Fri–Sat nights", color: t.weekend },
      ];
      const cap = outlierCap(pts.map((p) => p.price));
      const off = (p) => cap != null && p.price > cap;
      const traces = series.map((s) => {
        const sel = pts.filter((p) => p.we === s.key);
        return {
          type: "scatter", mode: "markers", name: s.name, x: sel.map((p) => (off(p) ? cap : p.price)),
          y: sel.map((p) => p[yKey]), cliponaxis: false,
          marker: {
            color: s.color, size: sel.map((p) => (p.ev.length || off(p) ? 12 : 9)),
            symbol: sel.map((p) => (off(p) ? "triangle-right" : p.ev.length ? "diamond" : "circle")),
            line: { width: 2, color: t.ring },
          },
          customdata: sel.map((p) => [p.d, p.label, money(p.price), p.rooms, ordinal(p.pace),
                                      p.ev.length ? "<br>No school next day: " + p.ev.join("; ") : ""]),
          hovertemplate: "%{customdata[1]}<br><b>%{customdata[2]}</b> lowest rate · <b>%{customdata[3]}</b> rooms left · pace %{customdata[4]}%{customdata[5]}<extra>%{fullData.name}</extra>",
        };
      });
      traces.push({ type: "scatter", mode: "markers", name: "No school next day", x: [null], y: [null],
                    marker: { symbol: "diamond", size: 12, color: t.muted, line: { width: 2, color: t.ring } },
                    hoverinfo: "skip" });
      const xs = pts.map((p) => p.price), ys = pts.map((p) => p[yKey]);
      const shapes = [], annotations = [];
      const nOff = pts.filter(off).length;
      if (pts.length >= 4) {
        const mx = median(xs), my = median(ys);
        const quietUp = yKey === "rooms";  // more rooms left = quieter; lower pace = quieter
        shapes.push({ type: "rect", layer: "below", line: { width: 0 }, fillcolor: t.quadrant, xref: "x", yref: "y",
                      x0: Math.min(...xs) - 10000, x1: mx,
                      y0: quietUp ? my : Math.min(...ys) - 1000, y1: quietUp ? Math.max(...ys) + 1000 : my });
        shapes.push({ type: "line", xref: "x", yref: "paper", x0: mx, x1: mx, y0: 0, y1: 1, line: { width: 1, color: t.axis } });
        shapes.push({ type: "line", xref: "paper", yref: "y", x0: 0, x1: 1, y0: my, y1: my, line: { width: 1, color: t.axis } });
        annotations.push({ xref: "paper", yref: "paper", x: 0.005, y: quietUp ? 0.995 : 0.005, xanchor: "left",
                           yanchor: quietUp ? "top" : "bottom", showarrow: false, bgcolor: t.surface, opacity: 0.9,
                           borderpad: 3, text: "cheaper & quieter", font: { size: 11, color: t.ink2 } });
      }
      const pad = (arr, frac) => { const lo = Math.min(...arr), hi = Math.max(...arr), d = (hi - lo || 1) * frac; return [lo - d, hi + d]; };
      const xShown = cap != null ? xs.map((x) => Math.min(x, cap)) : xs;
      GWT.plot(spec.chart, traces, layout(t, {
        showlegend: true, hovermode: "closest", shapes, annotations,
        margin: { l: 62, r: 24, t: 40, b: 50 },
        xaxis: { title: { text: "Lowest nightly rate" }, tickprefix: "$", range: pts.length ? pad(xShown, 0.04) : undefined },
        yaxis: { title: { text: yTitle }, range: pts.length ? pad(ys, 0.06) : undefined },
      }), goDate);
      const hidden = spec.points.filter((p) => p.i < state.window &&
        (state.nights === "all" || (state.nights === "weekend") === p.we)).length - pts.length;
      const note = root.querySelector(".chart-note");
      if (note) {
        note.textContent = pts.length + " nights shown" +
          (hidden ? " · " + hidden + " without a price" + (yKey === "pace" ? " or pace" : "") + " (sold out or n/a) not shown" : "") +
          (nOff ? " · ▶ " + nOff + " night" + (nOff > 1 ? "s" : "") + " above " + money0(cap) + " drawn at the right edge (hover for the price)" : "") +
          ". Shaded: cheaper than the median and " + (yKey === "rooms" ? "more rooms left" : "lower pace") + " than the median.";
      }
      if (tableBox && tableBox.open) fillTable();
    }
    function fillTable() {
      const body = tableBox.querySelector("tbody");
      body.textContent = "";
      filtered().forEach((p) => {
        const tr = document.createElement("tr");
        const cells = [p.label, money(p.price), String(p.rooms), ordinal(p.pace), p.ev.join("; ")];
        cells.forEach((text, i) => {
          const td = document.createElement("td");
          if (i === 0) {
            const a = document.createElement("a");
            a.href = "/date/" + p.d;
            a.textContent = text;
            td.appendChild(a);
          } else td.textContent = text;
          if (i > 0 && i < 4) td.className = "num";
          tr.appendChild(td);
        });
        body.appendChild(tr);
      });
    }
    draw();
  };

  // ------------------------------------------------------------------ Seasonality
  const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

  function weekRange(year, week) {
    const start = new Date(Date.UTC(year, 0, 1 + (week - 1) * 7));
    const end = new Date(Date.UTC(year, 0, Math.min(week * 7, week === 52 ? 366 : week * 7)));
    const f = (d) => MONTHS[d.getUTCMonth()] + " " + d.getUTCDate();
    return f(start) + "–" + f(end);
  }

  GWT.finalFill = function (elId, panel, spec) {
    const t = theme();
    const n = panel.series.length;
    const colours = panel.series.map((s, i) => (i === n - 1 ? t.emph : i === n - 2 ? t.old1 : t.old2));
    const weeks = Array.from({ length: 52 }, (_, i) => i + 1);
    const traces = panel.series.map((s, i) => ({
      x: weeks, y: s.values, mode: "lines+markers", name: String(s.year), connectgaps: false,
      line: { width: i === n - 1 ? 2.5 : 2, color: colours[i] },
      marker: { size: 6, color: colours[i], line: { width: 1.5, color: t.ring } },
      customdata: weeks.map((w, j) => [weekRange(s.year, w), s.counts[j]]),
      hovertemplate: "%{customdata[0]}, " + s.year + "<br><b>%{y:.0f}%</b> sold at arrival (avg of %{customdata[1]} nights)<extra></extra>",
    }));
    const annotations = [];
    panel.series.forEach((s, i) => {
      let last = -1;
      s.values.forEach((v, j) => { if (v != null) last = j; });
      if (last >= 0) annotations.push({ x: last + 1, y: s.values[last], text: String(s.year), xanchor: "left", xshift: 6,
                                        showarrow: false, font: { size: 11, color: t.ink2 } });
    });
    const years = new Set(panel.series.map((s) => s.year));
    const shapes = spec.week_spans.filter((w) => years.has(w.year)).map((w) => ({
      type: "rect", layer: "below", xref: "x", yref: "paper", x0: w.x0, x1: w.x1, y0: 0, y1: 1,
      fillcolor: t.closure, line: { width: 0 },
    }));
    GWT.plot(elId, traces, layout(t, {
      showlegend: n > 1, hovermode: "closest", shapes, annotations,
      margin: { l: 56, r: 40, t: n > 1 ? 30 : 10, b: 40 },
      xaxis: { range: [0.5, 53], tickvals: spec.month_ticks, ticktext: MONTHS, ticks: "outside" },
      yaxis: { title: { text: "Avg % sold at arrival (est.)" }, range: [0, 100], ticksuffix: "%" },
    }));
  };

  GWT.forecast = function (elId, spec) {
    const t = theme();
    const rows = spec.forecast;
    const mk = (key, name, colour) => ({
      x: rows.map((r) => r.week_start), y: rows.map((r) => r[key]), name, mode: "lines+markers", connectgaps: false,
      line: { width: 2, color: colour }, marker: { size: 7, color: colour, line: { width: 1.5, color: t.ring } },
      customdata: rows.map((r) => [r.label, money(r[key]), r[key + "_nights"], r[key + "_sold_out"]]),
      hovertemplate: "Week of %{customdata[0]}<br><b>%{customdata[1]}</b> avg lowest rate (%{customdata[2]} nights, %{customdata[3]} sold out not averaged)<extra>" + name + "</extra>",
    });
    const traces = [mk("weekday", "Sun–Thu nights", t.weekday), mk("weekend", "Fri–Sat nights", t.weekend)];
    const shapes = spec.forecast_spans.map((s) => ({
      type: "rect", layer: "below", xref: "x", yref: "paper", x0: s.start, x1: addDay(s.end), y0: 0, y1: 1,
      fillcolor: t.closure, line: { width: 0 },
    }));
    // Name the multi-night breaks (only where there's room); stagger labels so neighbours don't collide.
    const el = document.getElementById(elId);
    const wide = el && el.clientWidth >= 900;
    const annotations = spec.forecast_spans.filter((s) => wide && s.nights >= 3).map((s, i) => ({
      x: s.start, xref: "x", yref: "paper", y: 1, yanchor: "bottom", xanchor: "left", showarrow: false,
      yshift: (i % 2) * 14, text: s.title, hovertext: s.labels.join("<br>"), font: { size: 10, color: t.ink2 },
    }));
    const values = rows.flatMap((r) => [r.weekday, r.weekend]).filter((v) => v != null);
    const cap = outlierCap(values);
    let yrange;
    if (cap != null) {
      yrange = [Math.min(...values) * 0.92, cap * 1.04];
      ["weekday", "weekend"].forEach((key) => {
        let run = null;
        rows.forEach((r, j) => {
          const over = r[key] != null && r[key] > cap;
          if (over && !run) run = { x: r.week_start, max: r[key] };
          else if (over) run.max = Math.max(run.max, r[key]);
          if (run && (!over || j === rows.length - 1)) {
            annotations.push({ x: run.x, y: cap * 1.04, xref: "x", yref: "y", yanchor: "top", xanchor: "left",
                               showarrow: false, text: "▲ " + money0(run.max), font: { size: 11, color: t.ink2 } });
            run = null;
          }
        });
      });
    }
    GWT.plot(elId, traces, layout(t, {
      showlegend: true, hovermode: "x unified", shapes, annotations,
      margin: { l: 62, r: 20, t: 58, b: 44 },
      legend: { orientation: "h", x: 0, y: 1.16, yanchor: "bottom", font: { size: 12, color: t.ink2 } },
      xaxis: { type: "date", tickformat: "%b %Y", hoverformat: "%b %-d" },
      yaxis: { title: { text: "Average lowest nightly rate" }, tickprefix: "$", range: yrange },
    }));
  };
  function addDay(iso) {
    const d = new Date(iso + "T00:00:00Z");
    d.setUTCDate(d.getUTCDate() + 1);
    return d.toISOString().slice(0, 10);
  }

  // Calendar metric toggle: keep ?metric= in the URL so a view can be shared.
  document.addEventListener("change", (e) => {
    if (e.target && e.target.name === "metric") {
      const url = new URL(window.location.href);
      url.searchParams.set("metric", e.target.value);
      window.history.replaceState(null, "", url);
    }
  });

  // Charts redraw with the right colours when the OS theme flips.
  if (window.matchMedia) {
    window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => {
      if (GWT.redraw) GWT.redraw();
    });
  }

  window.GWT = GWT;
})();
