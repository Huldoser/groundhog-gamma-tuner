// History screen and weather location. Loaded after app.js and uses its helpers
// ($, api, openModal, closeModal, showNotice, setFormError, hideMenu, hideSettingsMenu).

const SERIES_VARS = [
  "--series-1", "--series-2", "--series-3", "--series-4",
  "--series-5", "--series-6", "--series-7", "--series-8",
];
// Blue sequential steps, dark to light. On this dark panel the dark end recedes.
const HEAT_RAMP = [
  "#104281", "#184f95", "#1c5cab", "#256abf", "#2a78d6",
  "#3987e5", "#5598e7", "#6da7ec", "#86b6ef", "#9ec5f4",
];
const SVG_NS = "http://www.w3.org/2000/svg";
const HISTORY_REFRESH_MS = 60000;
const FILTER_KEY = "groundhog-history-filters";
const TIME_STEPS = [3600, 3 * 3600, 6 * 3600, 12 * 3600, 86400, 2 * 86400, 7 * 86400, 14 * 86400, 30 * 86400];

const historyState = {
  view: "miners",
  ip: "",
  period: "7d",
  metric: "good_hashrate",
  data: null,
  loading: false,
  reload: false,
  timer: null,
  geometry: null,
  resizeTimer: null,
};

function readSavedFilters() {
  try {
    const saved = JSON.parse(localStorage.getItem(FILTER_KEY) || "{}");
    if (saved && typeof saved === "object") {
      if (typeof saved.ip === "string") historyState.ip = saved.ip;
      if (typeof saved.period === "string") historyState.period = saved.period;
      if (typeof saved.metric === "string") historyState.metric = saved.metric;
    }
  } catch (_error) {
    // Storage can be unavailable. The defaults stand.
  }
}

function saveFilters() {
  try {
    localStorage.setItem(FILTER_KEY, JSON.stringify({
      ip: historyState.ip,
      period: historyState.period,
      metric: historyState.metric,
    }));
  } catch (_error) {
    // Not saved this time. The screen still works.
  }
}

function cssVar(name) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

function seriesColor(slot) {
  return cssVar(SERIES_VARS[Math.abs(slot || 0) % SERIES_VARS.length]);
}

function svg(tag, attrs, parent) {
  const element = document.createElementNS(SVG_NS, tag);
  Object.entries(attrs || {}).forEach(([key, value]) => element.setAttribute(key, value));
  if (parent) parent.appendChild(element);
  return element;
}

function finite(value) {
  return value != null && Number.isFinite(Number(value));
}

function formatValue(value, digits) {
  if (!finite(value)) return "–";
  return Number(value).toLocaleString(undefined, {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  });
}

function formatTemp(value) {
  return finite(value) ? `${formatValue(value, 1)} °C` : "–";
}

function pad2(value) {
  return String(value).padStart(2, "0");
}

function formatWhen(ts) {
  return new Date(ts * 1000).toLocaleString(undefined, {
    weekday: "short",
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

function formatDate(ts) {
  return new Date(ts * 1000).toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

function hourRange(hour) {
  return `${pad2(hour)}:00–${pad2((hour + 1) % 24)}:00`;
}

// GH/s turns into TH/s once the numbers on screen pass 1,000.
function metricScale(meta, values) {
  const peak = Math.max(0, ...values.filter(finite).map(Number));
  if (meta.unit === "GH/s" && peak >= 1000) return { div: 1000, unit: "TH/s", digits: 2 };
  return { div: 1, unit: meta.unit, digits: meta.digits };
}

function scaled(value, scale) {
  return finite(value) ? formatValue(Number(value) / scale.div, scale.digits) : "–";
}

function niceStep(range, count) {
  const raw = range / Math.max(count, 1);
  const power = 10 ** Math.floor(Math.log10(raw));
  const unit = raw / power;
  const step = unit <= 1 ? 1 : unit <= 2 ? 2 : unit <= 2.5 ? 2.5 : unit <= 5 ? 5 : 10;
  return step * power;
}

function niceTicks(min, max, count) {
  let low = min;
  let high = max;
  if (!(high > low)) {
    const pad = Math.abs(low) * 0.05 || 1;
    low -= pad;
    high += pad;
  }
  const step = niceStep(high - low, count);
  const start = Math.floor(low / step) * step;
  const end = Math.ceil(high / step) * step;
  const ticks = [];
  for (let value = start; value <= end + step / 2; value += step) ticks.push(Number(value.toFixed(10)));
  return { low: start, high: end, ticks, step };
}

function timeTicks(t0, t1, maxTicks) {
  const span = Math.max(t1 - t0, 1);
  const step = TIME_STEPS.find((candidate) => span / candidate <= maxTicks) || TIME_STEPS[TIME_STEPS.length - 1];
  const cursor = new Date(t0 * 1000);
  if (step < 86400) {
    const hours = step / 3600;
    cursor.setMinutes(0, 0, 0);
    while (cursor.getTime() / 1000 < t0 || cursor.getHours() % hours) cursor.setHours(cursor.getHours() + 1);
  } else {
    cursor.setHours(0, 0, 0, 0);
    while (cursor.getTime() / 1000 < t0) cursor.setDate(cursor.getDate() + 1);
  }
  const ticks = [];
  while (cursor.getTime() / 1000 <= t1 && ticks.length < 60) {
    const ts = cursor.getTime() / 1000;
    let label;
    if (step < 86400) {
      label = span > 86400 && cursor.getHours() === 0
        ? cursor.toLocaleDateString(undefined, { weekday: "short", day: "numeric" })
        : `${pad2(cursor.getHours())}:00`;
    } else if (span <= 16 * 86400) {
      label = cursor.toLocaleDateString(undefined, { weekday: "short", day: "numeric" });
    } else {
      label = cursor.toLocaleDateString(undefined, { month: "short", day: "numeric" });
    }
    ticks.push({ ts, label });
    if (step < 86400) cursor.setHours(cursor.getHours() + step / 3600);
    else cursor.setDate(cursor.getDate() + step / 86400);
  }
  return ticks;
}

// Index of the point nearest to `ts` in a list sorted by time.
function nearestIndex(points, ts) {
  let low = 0;
  let high = points.length - 1;
  if (high < 0) return -1;
  while (low < high) {
    const middle = (low + high) >> 1;
    if (points[middle][0] < ts) low = middle + 1;
    else high = middle;
  }
  if (low > 0 && Math.abs(points[low - 1][0] - ts) <= Math.abs(points[low][0] - ts)) return low - 1;
  return low;
}

function pathFor(points, x, y, gap) {
  let path = "";
  let previous = null;
  points.forEach(([ts, value]) => {
    if (!finite(value)) {
      previous = null;
      return;
    }
    const command = previous == null || ts - previous > gap ? "M" : "L";
    path += `${command}${x(ts).toFixed(1)},${y(value).toFixed(1)}`;
    previous = ts;
  });
  return path;
}

function typicalGap(points) {
  const gaps = [];
  for (let index = 1; index < points.length; index += 1) gaps.push(points[index][0] - points[index - 1][0]);
  if (!gaps.length) return 3600;
  gaps.sort((a, b) => a - b);
  return gaps[Math.floor(gaps.length / 2)];
}

function chartEmpty(container, message) {
  const note = document.createElement("p");
  note.className = "chart-empty";
  note.textContent = message;
  container.replaceChildren(note);
}

/* Tooltip */

function tipRow(parent, color, value, label) {
  const row = document.createElement("div");
  row.className = "tip-row";
  if (color) {
    const key = document.createElement("i");
    key.className = "tip-key";
    key.style.background = color;
    row.appendChild(key);
  }
  const strong = document.createElement("strong");
  strong.textContent = value;
  row.appendChild(strong);
  if (label) {
    const name = document.createElement("span");
    name.textContent = label;
    row.appendChild(name);
  }
  parent.appendChild(row);
}

function showTip(title, rows, clientX, clientY) {
  const tip = $("chart-tip");
  tip.replaceChildren();
  const head = document.createElement("div");
  head.className = "tip-title";
  head.textContent = title;
  tip.appendChild(head);
  rows.forEach((row) => tipRow(tip, row.color, row.value, row.label));
  tip.hidden = false;
  const box = tip.getBoundingClientRect();
  let left = clientX + 16;
  let top = clientY + 16;
  if (left + box.width > window.innerWidth - 8) left = clientX - box.width - 16;
  if (top + box.height > window.innerHeight - 8) top = clientY - box.height - 16;
  tip.style.left = `${Math.max(8, left)}px`;
  tip.style.top = `${Math.max(8, top)}px`;
}

function hideTip() {
  $("chart-tip").hidden = true;
  const geometry = historyState.geometry;
  if (geometry) geometry.hoverLayers.forEach((layer) => layer.replaceChildren());
}

/* Filters and header */

function syncFilters(data) {
  const select = $("history-miner");
  const options = [{ ip: "", name: "All miners" }, ...(data.miners || [])];
  const key = options.map((miner) => `${miner.ip}|${miner.name}`).join(",");
  if (select.dataset.key !== key) {
    select.replaceChildren();
    options.forEach((miner) => {
      const option = document.createElement("option");
      option.value = miner.ip;
      option.textContent = miner.name;
      select.appendChild(option);
    });
    select.dataset.key = key;
  }
  select.value = data.filters.ip || "";
  historyState.ip = data.filters.ip || "";
  historyState.period = data.filters.period;
  historyState.metric = data.filters.metric;
  syncSegments();
}

function syncSegments() {
  document.querySelectorAll("#history-period button").forEach((button) => {
    button.setAttribute("aria-checked", button.dataset.period === historyState.period ? "true" : "false");
  });
  document.querySelectorAll("#history-metric button").forEach((button) => {
    button.setAttribute("aria-checked", button.dataset.metric === historyState.metric ? "true" : "false");
  });
}

function shortPlace(name) {
  return String(name || "").split(",")[0].trim();
}

function renderLocationChip(location, weather) {
  $("history-place").textContent = location ? location.name : "Set weather location";
  $("history-now").textContent = weather && finite(weather.temp)
    ? `${formatTemp(weather.temp)}${weather.label ? ` · ${weather.label}` : ""}`
    : "";
}

function renderWeatherChip(weather) {
  const chip = $("weather-chip");
  if (!chip) return;
  if (!weather || !finite(weather.temp)) {
    chip.hidden = true;
    return;
  }
  chip.replaceChildren();
  const place = shortPlace(weather.place);
  if (place) chip.append(document.createTextNode(`${place} `));
  const temp = document.createElement("strong");
  temp.textContent = formatTemp(weather.temp);
  chip.appendChild(temp);
  if (weather.label) chip.append(document.createTextNode(` ${weather.label}`));
  const details = [];
  if (finite(weather.apparent)) details.push(`Feels like ${formatTemp(weather.apparent)}`);
  if (finite(weather.humidity)) details.push(`Humidity ${formatValue(weather.humidity, 0)}%`);
  if (finite(weather.wind)) details.push(`Wind ${formatValue(weather.wind, 0)} km/h`);
  details.push("Click to change the location");
  chip.title = details.join("\n");
  chip.hidden = false;
}

function renderNotice(data) {
  const notice = $("history-notice");
  notice.replaceChildren();
  let message = "";
  let action = null;
  if (!data.location) {
    message = "Set a weather location so each sample is saved with the weather outside.";
    action = { label: "Set Location", run: openLocation };
  } else if (!data.sample_count) {
    message = `History starts now. A sample is saved for every miner each ${data.sample_minutes} minutes while this window is open, the first one ${data.sample_minutes} minutes after it opens.`;
  } else if (data.filters.period === "reset" && !Object.keys(data.resets || {}).length) {
    message = "No Reset to Baseline is recorded yet, so Since reset shows everything.";
  }
  if (!message) {
    notice.hidden = true;
    return;
  }
  const text = document.createElement("span");
  text.textContent = message;
  notice.appendChild(text);
  if (action) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "accent";
    button.textContent = action.label;
    button.addEventListener("click", action.run);
    notice.appendChild(button);
  }
  notice.hidden = false;
}

/* Stat tiles */

function addKpi(parent, label, value, unit, sub, tone) {
  const tile = document.createElement("div");
  tile.className = "kpi";
  const name = document.createElement("span");
  name.className = "kpi-label";
  name.textContent = label;
  tile.appendChild(name);
  const figure = document.createElement("span");
  figure.className = tone ? `kpi-value ${tone}` : "kpi-value";
  figure.textContent = value;
  if (unit) {
    const small = document.createElement("small");
    small.textContent = unit;
    figure.appendChild(small);
  }
  tile.appendChild(figure);
  const note = document.createElement("span");
  note.className = "kpi-sub";
  note.textContent = sub || "";
  tile.appendChild(note);
  parent.appendChild(tile);
}

function conditionsText(best) {
  const parts = [formatWhen(best.ts)];
  if (finite(best.outdoor_temp)) parts.push(`${formatTemp(best.outdoor_temp)} outside`);
  if (best.sky_label) parts.push(best.sky_label);
  if (finite(best.frequency) && finite(best.voltage)) {
    parts.push(`${formatValue(best.frequency, 0)} MHz / ${formatValue(best.voltage, 0)} mV`);
  }
  if (finite(best.asic_temp)) parts.push(`ASIC ${formatTemp(best.asic_temp)}`);
  return parts.join(" · ");
}

function bestLabel(data) {
  if (data.filters.ip) return `Best ${data.meta.label.toLowerCase()}`;
  if (data.metric === "good_hashrate") return "Best fleet total";
  if (data.metric === "efficiency") return "Best fleet efficiency";
  return "Best average clock";
}

function renderKpis(data, scale) {
  const row = $("history-kpis");
  row.replaceChildren();
  const meta = data.meta;
  if (data.best) {
    addKpi(row, bestLabel(data), scaled(data.best.value, scale), scale.unit, conditionsText(data.best), "good");
  } else {
    addKpi(row, bestLabel(data), "No result yet", "", "Needs a sample whose setpoint held for 10 minutes.", "quiet");
  }
  if (finite(data.typical)) {
    addKpi(
      row,
      "Typical",
      scaled(data.typical, scale),
      scale.unit,
      `Median of ${data.settled_count.toLocaleString()} settled samples${meta.higher_is_better ? "" : " · lower is better"}`,
    );
  } else {
    addKpi(row, "Typical", "No result yet", "", "The median of settled samples shows here.", "quiet");
  }
  if (finite(data.trend_per_c)) {
    // A fleet slope is often tens of GH/s even when totals are in TH/s.
    const slopeScale = Math.abs(data.trend_per_c) < scale.div
      ? { div: 1, unit: meta.unit, digits: meta.digits }
      : scale;
    const value = Number(data.trend_per_c) / slopeScale.div;
    const sign = value > 0 ? "+" : value < 0 ? "−" : "";
    addKpi(
      row,
      "Each 1 °C warmer outside",
      `${sign}${formatValue(Math.abs(value), slopeScale.digits)}`,
      slopeScale.unit,
      "Straight-line fit over settled samples",
    );
  } else {
    addKpi(
      row,
      "Each 1 °C warmer outside",
      "Not enough data",
      "",
      "Needs 2 hours of settled samples across at least 3 °C outside.",
      "quiet",
    );
  }
  if (finite(data.outdoor_low)) {
    addKpi(
      row,
      "Outdoor range",
      `${formatValue(data.outdoor_low, 0)} to ${formatValue(data.outdoor_high, 0)}`,
      "°C",
      `${formatValue(data.hours, 1)} h recorded${data.first ? ` since ${formatDate(data.first)}` : ""}`,
    );
  } else {
    addKpi(
      row,
      "Outdoor range",
      "No weather yet",
      "",
      `${formatValue(data.hours, 1)} h recorded`,
      "quiet",
    );
  }
}

/* Over-time chart and outdoor strip, sharing one time axis and crosshair */

function chartSeries(data) {
  const miners = data.miners || [];
  const list = [];
  miners.forEach((miner) => {
    const points = (data.series || {})[miner.ip];
    if (!points || !points.length) return;
    if (data.filters.ip && data.filters.ip !== miner.ip) return;
    list.push({ ip: miner.ip, name: miner.name, color: seriesColor(miner.slot), points });
  });
  return list;
}

function renderLegend(series) {
  const legend = $("trend-legend");
  legend.replaceChildren();
  if (series.length < 2) return;
  series.forEach((item) => {
    const button = document.createElement("button");
    button.type = "button";
    button.title = `Show only ${item.name}`;
    const key = document.createElement("i");
    key.className = "legend-key";
    key.style.background = item.color;
    button.appendChild(key);
    button.append(document.createTextNode(item.name));
    button.addEventListener("click", () => setHistoryFilter({ ip: item.ip }));
    legend.appendChild(button);
  });
}

function drawAxisX(group, ticks, x, top, bottom) {
  ticks.forEach((tick) => {
    const at = x(tick.ts);
    svg("line", { x1: at, x2: at, y1: top, y2: bottom }, group.grid);
    const label = svg("text", { x: at, y: bottom + 16, "text-anchor": "middle" }, group.ticks);
    label.textContent = tick.label;
  });
}

function drawLineChart(container, options) {
  const width = Math.max(container.clientWidth, 320);
  const height = options.height;
  const margin = { top: 10, right: 18, bottom: 24, left: 58 };
  const plotRight = width - margin.right;
  const plotBottom = height - margin.bottom;
  const root = svg("svg", { viewBox: `0 0 ${width} ${height}`, height, role: "img", "aria-label": options.label });
  const grid = svg("g", { class: "grid" }, root);
  const ticksGroup = svg("g", { class: "tick" }, root);
  const values = options.series.flatMap((item) => item.points.map((point) => point[1])).filter(finite);
  if (!values.length) return null;
  const yTicks = niceTicks(Math.min(...values), Math.max(...values), options.yCount);
  const x = (ts) => margin.left + ((ts - options.t0) / Math.max(options.t1 - options.t0, 1)) * (plotRight - margin.left);
  const y = (value) => plotBottom - ((value - yTicks.low) / Math.max(yTicks.high - yTicks.low, 1e-9)) * (plotBottom - margin.top);
  yTicks.ticks.forEach((tick) => {
    const at = y(tick);
    if (tick !== yTicks.low) svg("line", { x1: margin.left, x2: plotRight, y1: at, y2: at }, grid);
    const label = svg("text", { x: margin.left - 8, y: at + 4, "text-anchor": "end" }, ticksGroup);
    label.textContent = options.format(tick);
  });
  drawAxisX({ grid, ticks: ticksGroup }, options.xTicks, x, margin.top, plotBottom);
  svg("line", { class: "baseline", x1: margin.left, x2: plotRight, y1: plotBottom, y2: plotBottom }, root);
  options.series.forEach((item) => {
    const gap = Math.max(typicalGap(item.points) * 3, options.minGap);
    if (options.area) {
      const line = pathFor(item.points, x, y, gap);
      const segments = line.split("M").filter(Boolean);
      segments.forEach((segment) => {
        const coords = segment.split("L");
        const first = coords[0].split(",")[0];
        const last = coords[coords.length - 1].split(",")[0];
        svg("path", {
          d: `M${first},${plotBottom}L${segment}L${last},${plotBottom}Z`,
          fill: item.color,
          "fill-opacity": "0.1",
          stroke: "none",
        }, root);
      });
    }
    svg("path", { class: "series", d: pathFor(item.points, x, y, gap), stroke: item.color }, root);
    const lastPoint = [...item.points].reverse().find((point) => finite(point[1]));
    if (lastPoint && options.endDots) {
      svg("circle", { class: "hover-dot", cx: x(lastPoint[0]), cy: y(lastPoint[1]), r: 4, fill: item.color }, root);
    }
  });
  const hoverLayer = svg("g", {}, root);
  const overlay = svg("rect", {
    x: margin.left,
    y: margin.top,
    width: Math.max(plotRight - margin.left, 1),
    height: Math.max(plotBottom - margin.top, 1),
    fill: "transparent",
  }, root);
  container.replaceChildren(root);
  return { root, overlay, hoverLayer, x, y, margin, plotRight, plotBottom, t0: options.t0, t1: options.t1, width };
}

function drawTrend(data, scale) {
  const series = chartSeries(data);
  const trend = $("trend-chart");
  const outdoorBox = $("outdoor-chart");
  renderLegend(series);
  historyState.geometry = null;
  const meta = data.meta;
  $("trend-title").textContent = data.filters.ip
    ? `${meta.label} over time`
    : `${meta.label} by miner over time`;
  $("trend-sub").textContent = data.filters.ip
    ? `${scale.unit}${meta.higher_is_better ? "" : ", lower is better"}`
    : `${scale.unit}${meta.higher_is_better ? "" : ", lower is better"} · click a name to show one miner`;
  if (!series.length || !data.first) {
    chartEmpty(trend, "Nothing recorded for this period yet.");
    chartEmpty(outdoorBox, data.location ? "Outdoor readings appear with the first samples." : "Set a weather location to see the weather here.");
    return;
  }
  let t0 = data.first;
  let t1 = data.last;
  if (t1 - t0 < 3600) {
    t0 -= 1800;
    t1 += 1800;
  }
  const width = Math.max(trend.clientWidth, 320);
  const xTicks = timeTicks(t0, t1, Math.max(3, Math.floor((width - 80) / 96)));
  const minGap = (data.sample_minutes || 10) * 60 * 3;
  const scaledSeries = series.map((item) => ({
    ...item,
    points: item.points.map(([ts, value]) => [ts, finite(value) ? value / scale.div : null]),
  }));
  const main = drawLineChart(trend, {
    height: 240,
    series: scaledSeries,
    t0,
    t1,
    xTicks,
    yCount: 5,
    minGap,
    endDots: true,
    label: `${meta.label} over time`,
    format: (value) => formatValue(value, scale.digits > 0 && Math.abs(value) < 100 ? scale.digits : 0),
  });
  if (!main) {
    chartEmpty(trend, `No ${meta.label.toLowerCase()} readings in this period yet.`);
    chartEmpty(outdoorBox, "");
    return;
  }
  const outdoorPoints = (data.outdoor || []).filter((point) => finite(point[1]));
  let strip = null;
  if (outdoorPoints.length) {
    strip = drawLineChart(outdoorBox, {
      height: 110,
      series: [{ name: "Outdoor", color: cssVar("--outdoor"), points: data.outdoor }],
      t0,
      t1,
      xTicks,
      yCount: 3,
      minGap,
      area: true,
      endDots: false,
      label: "Outdoor temperature over time",
      format: (value) => `${formatValue(value, 0)}°`,
    });
  }
  if (!strip) {
    chartEmpty(outdoorBox, data.location ? "No outdoor readings in this period yet." : "Set a weather location to see the weather here.");
  }
  historyState.geometry = {
    series: scaledSeries,
    outdoor: outdoorPoints,
    scale,
    main,
    strip,
    hoverLayers: [main.hoverLayer, strip ? strip.hoverLayer : null].filter(Boolean),
  };
  [main, strip].filter(Boolean).forEach((chart) => {
    chart.overlay.addEventListener("pointermove", (event) => hoverTrend(chart, event));
    chart.overlay.addEventListener("pointerleave", hideTip);
  });
}

function hoverTrend(chart, event) {
  const geometry = historyState.geometry;
  if (!geometry) return;
  const box = chart.root.getBoundingClientRect();
  const px = ((event.clientX - box.left) / box.width) * chart.width;
  const ratio = (px - chart.margin.left) / Math.max(chart.plotRight - chart.margin.left, 1);
  const ts = chart.t0 + Math.min(Math.max(ratio, 0), 1) * (chart.t1 - chart.t0);
  const main = geometry.main;
  const rows = [];
  let snapTs = null;
  geometry.series.forEach((item) => {
    const index = nearestIndex(item.points, ts);
    if (index < 0) return;
    const point = item.points[index];
    if (snapTs == null || Math.abs(point[0] - ts) < Math.abs(snapTs - ts)) snapTs = point[0];
  });
  if (snapTs == null) return;
  geometry.hoverLayers.forEach((layer) => layer.replaceChildren());
  const tolerance = Math.max(typicalGap(geometry.series[0].points) * 1.5, 900);
  geometry.series.forEach((item) => {
    const index = nearestIndex(item.points, snapTs);
    const point = index >= 0 ? item.points[index] : null;
    if (!point || !finite(point[1]) || Math.abs(point[0] - snapTs) > tolerance) return;
    svg("circle", { class: "hover-dot", cx: main.x(point[0]), cy: main.y(point[1]), r: 4.5, fill: item.color }, main.hoverLayer);
    rows.push({ color: item.color, value: `${formatValue(point[1], geometry.scale.digits)} ${geometry.scale.unit}`, label: item.name, sort: point[1] });
  });
  rows.sort((a, b) => b.sort - a.sort);
  const x = main.x(snapTs);
  svg("line", { class: "crosshair", x1: x, x2: x, y1: main.margin.top, y2: main.plotBottom }, main.hoverLayer);
  if (geometry.strip) {
    const strip = geometry.strip;
    svg("line", { class: "crosshair", x1: x, x2: x, y1: strip.margin.top, y2: strip.plotBottom }, strip.hoverLayer);
    const index = nearestIndex(geometry.outdoor, snapTs);
    const point = index >= 0 ? geometry.outdoor[index] : null;
    if (point && Math.abs(point[0] - snapTs) <= Math.max(tolerance, 3600)) {
      svg("circle", { class: "hover-dot", cx: strip.x(point[0]), cy: strip.y(point[1]), r: 4.5, fill: cssVar("--outdoor") }, strip.hoverLayer);
      rows.push({ color: cssVar("--outdoor"), value: formatTemp(point[1]), label: "outside" });
    }
  }
  showTip(formatWhen(snapTs), rows, event.clientX, event.clientY);
}

/* Heatmap: hour of day by outdoor temperature */

function heatColor(value, low, high, higherIsBetter) {
  let ratio = high > low ? (value - low) / (high - low) : 1;
  if (!higherIsBetter) ratio = 1 - ratio;
  const index = Math.round(Math.min(Math.max(ratio, 0), 1) * (HEAT_RAMP.length - 1));
  return HEAT_RAMP[index];
}

function drawHeatmap(data, scale) {
  const container = $("heatmap");
  const legend = $("heat-legend");
  legend.replaceChildren();
  const heat = data.heatmap || { bands: [], cells: [] };
  const meta = data.meta;
  $("heat-sub").textContent = `Average ${meta.label.toLowerCase()} per hour of day and ${data.band_c} °C of outdoor temperature. Settled samples only.`;
  if (!heat.bands.length) {
    chartEmpty(
      container,
      data.location
        ? "Fills in once settled samples have outdoor readings."
        : "Set a weather location to fill this in.",
    );
    return;
  }
  const values = heat.cells.map((cell) => cell.value);
  const low = Math.min(...values);
  const high = Math.max(...values);
  const width = Math.max(container.clientWidth, 320);
  const margin = { top: 4, right: 4, bottom: 22, left: 70 };
  const cellWidth = (width - margin.left - margin.right) / 24;
  const cellHeight = Math.max(22, Math.min(34, cellWidth));
  const height = margin.top + heat.bands.length * cellHeight + margin.bottom;
  const root = svg("svg", { viewBox: `0 0 ${width} ${height}`, height, role: "img", "aria-label": "Results by hour and outdoor temperature" });
  const labels = svg("g", { class: "tick" }, root);
  const byKey = new Map(heat.cells.map((cell) => [`${cell.band}|${cell.hour}`, cell]));
  heat.bands.forEach((band, row) => {
    const top = margin.top + row * cellHeight;
    const label = svg("text", { x: margin.left - 10, y: top + cellHeight / 2 + 4, "text-anchor": "end" }, labels);
    label.textContent = band.label;
    for (let hour = 0; hour < 24; hour += 1) {
      const left = margin.left + hour * cellWidth;
      const cell = byKey.get(`${band.low}|${hour}`);
      const rect = svg("rect", {
        x: (left + 1).toFixed(1),
        y: (top + 1).toFixed(1),
        width: Math.max(cellWidth - 2, 1).toFixed(1),
        height: Math.max(cellHeight - 2, 1).toFixed(1),
        rx: 3,
      }, root);
      if (!cell) {
        rect.setAttribute("class", "heat-empty");
        continue;
      }
      rect.setAttribute("class", "heat-cell");
      rect.setAttribute("fill", heatColor(cell.value, low, high, meta.higher_is_better));
      rect.setAttribute("tabindex", "0");
      const title = `${hourRange(hour)} · ${band.label}`;
      const rows = [
        { value: `${scaled(cell.value, scale)} ${scale.unit}`, label: "average" },
        { value: `${cell.count}`, label: cell.count === 1 ? "sample" : "samples" },
      ];
      rect.addEventListener("pointermove", (event) => showTip(title, rows, event.clientX, event.clientY));
      rect.addEventListener("pointerleave", hideTip);
      rect.addEventListener("focus", () => {
        const box = rect.getBoundingClientRect();
        showTip(title, rows, box.right, box.bottom);
      });
      rect.addEventListener("blur", hideTip);
    }
  });
  for (let hour = 0; hour < 24; hour += 3) {
    const label = svg("text", {
      x: margin.left + hour * cellWidth + cellWidth / 2,
      y: height - 6,
      "text-anchor": "middle",
    }, labels);
    label.textContent = pad2(hour);
  }
  container.replaceChildren(root);

  const worse = meta.higher_is_better ? low : high;
  const better = meta.higher_is_better ? high : low;
  const start = document.createElement("span");
  start.textContent = `Worse ${scaled(worse, scale)}`;
  const ramp = document.createElement("i");
  ramp.className = "heat-ramp";
  ramp.style.background = `linear-gradient(to right, ${HEAT_RAMP.join(", ")})`;
  const end = document.createElement("span");
  end.textContent = `${scaled(better, scale)} ${scale.unit} better`;
  legend.append(start, ramp, end);
}

/* Best combinations */

function renderCombos(data, scale) {
  const body = $("combo-body");
  const empty = $("combo-empty");
  const meta = data.meta;
  body.replaceChildren();
  $("combo-metric").textContent = `Avg ${scale.unit}`;
  $("combo-sub").textContent = data.combo_total
    ? `${data.combo_total} combinations with at least 30 minutes of settled data${data.combo_total > data.combos.length ? `, top ${data.combos.length} shown` : ""}. ${meta.higher_is_better ? "Highest" : "Lowest"} average first.`
    : "";
  if (!data.combos.length) {
    empty.textContent = data.location
      ? "Each combination needs 30 minutes of settled samples with outdoor readings."
      : "Set a weather location to rank weather, time, and temperature.";
    empty.hidden = false;
    return;
  }
  empty.hidden = true;
  const values = data.combos.map((combo) => combo.value);
  const low = Math.min(...values);
  const high = Math.max(...values);
  data.combos.forEach((combo, index) => {
    const row = document.createElement("tr");
    if (index === 0) row.className = "top";
    const cells = [
      combo.band_label,
      combo.daypart_label,
      combo.sky_label,
      null,
      formatTemp(combo.asic_temp),
      null,
      formatValue(combo.hours, 1),
    ];
    cells.forEach((text, column) => {
      const cell = document.createElement("td");
      if (column < 4) cell.className = "left";
      if (column === 5) {
        if (finite(combo.frequency)) {
          addLine(cell, `${Math.round(combo.frequency)} MHz`);
          addLine(cell, `${Math.round(combo.voltage)} mV`, "muted");
        } else {
          cell.textContent = "–";
        }
      } else if (column === 3) {
        let ratio = high > low ? (combo.value - low) / (high - low) : 1;
        if (!meta.higher_is_better) ratio = 1 - ratio;
        const wrap = document.createElement("div");
        wrap.className = "combo-bar";
        const value = document.createElement("strong");
        value.textContent = `${scaled(combo.value, scale)}`;
        const track = document.createElement("span");
        track.className = "combo-track";
        const fill = document.createElement("span");
        fill.className = "combo-fill";
        fill.style.width = `${Math.round(18 + ratio * 82)}%`;
        track.appendChild(fill);
        wrap.append(value, track);
        cell.appendChild(wrap);
        cell.title = `${scaled(combo.value, scale)} ${scale.unit} average, best ${scaled(combo.best, scale)} ${scale.unit}`;
      } else {
        cell.textContent = text;
      }
      row.appendChild(cell);
    });
    body.appendChild(row);
  });
}

/* Loading and rendering */

function renderHistory(data) {
  syncFilters(data);
  renderLocationChip(data.location, data.weather);
  renderWeatherChip(data.weather);
  renderNotice(data);
  const values = [
    ...Object.values(data.series || {}).flatMap((points) => points.map((point) => point[1])),
    data.best ? data.best.value : null,
    data.typical,
    ...(data.combos || []).map((combo) => combo.value),
    ...((data.heatmap && data.heatmap.cells) || []).map((cell) => cell.value),
  ];
  const scale = metricScale(data.meta, values);
  hideTip();
  renderKpis(data, scale);
  drawTrend(data, scale);
  drawHeatmap(data, scale);
  renderCombos(data, scale);
  $("history-attribution").textContent = data.attribution || "";
}

async function loadHistory() {
  const bridge = api();
  if (!bridge) return;
  if (historyState.loading) {
    historyState.reload = true;
    return;
  }
  historyState.loading = true;
  $("history-body").classList.add("loading");
  try {
    const result = await bridge.get_history({
      ip: historyState.ip,
      period: historyState.period,
      metric: historyState.metric,
    });
    if (result && result.ok) {
      historyState.data = result;
      if (historyState.view === "history") renderHistory(result);
    } else if (result && result.notice) {
      showNotice(result.notice);
    }
  } catch (_error) {
    // Keep the last render. The next refresh tries again.
  } finally {
    historyState.loading = false;
    $("history-body").classList.remove("loading");
    if (historyState.reload) {
      historyState.reload = false;
      loadHistory();
    }
  }
}

function setHistoryFilter(change) {
  Object.assign(historyState, change);
  saveFilters();
  syncSegments();
  $("history-miner").value = historyState.ip;
  loadHistory();
}

function setView(view) {
  historyState.view = view;
  $("miners-view").hidden = view !== "miners";
  $("history-view").hidden = view !== "history";
  document.querySelectorAll(".view-tab").forEach((tab) => {
    tab.setAttribute("aria-selected", tab.dataset.view === view ? "true" : "false");
  });
  hideMenu();
  hideSettingsMenu();
  hideTip();
  clearInterval(historyState.timer);
  historyState.timer = null;
  if (view === "history") {
    if (historyState.data) renderHistory(historyState.data);
    loadHistory();
    historyState.timer = setInterval(loadHistory, HISTORY_REFRESH_MS);
  }
}

/* Weather location dialog */

function renderLocationDialog(result) {
  const place = result && result.location;
  const current = result && result.weather;
  $("location-name").textContent = place ? place.name : "Not set";
  let detail = "Pick a place so the History screen can compare runs with the weather.";
  if (place) {
    const parts = [`${Number(place.latitude).toFixed(3)}, ${Number(place.longitude).toFixed(3)}`];
    parts.push(place.source === "device" ? "from this device" : "picked from search");
    if (current && finite(current.temp)) parts.push(`${formatTemp(current.temp)}${current.label ? ` ${current.label.toLowerCase()}` : ""} now`);
    detail = parts.join(" · ");
  }
  $("location-detail").textContent = detail;
  if (result && "device_supported" in result) {
    $("location-detect").hidden = !result.device_supported;
  }
}

async function openLocation() {
  hideSettingsMenu();
  setFormError("location-error", "");
  $("location-results").hidden = true;
  $("location-results").replaceChildren();
  openModal("location");
  const bridge = api();
  if (!bridge) return;
  try {
    renderLocationDialog(await bridge.get_location());
  } catch (_error) {
    // The dialog still works without the current place.
  }
}

function locationSaved(result) {
  renderLocationDialog({ location: result.location, weather: null });
  $("location-results").hidden = true;
  $("location-results").replaceChildren();
  $("location-query").value = "";
  // The new place is read in the background. Pick up its weather shortly.
  setTimeout(() => {
    if (!$("location").hidden && api()) {
      api().get_location().then(renderLocationDialog).catch(() => {});
    }
    if (historyState.view === "history") loadHistory();
  }, 4000);
  if (historyState.view === "history") loadHistory();
}

async function detectLocation() {
  const bridge = api();
  if (!bridge) return;
  const button = $("location-detect");
  const label = button.textContent;
  button.disabled = true;
  button.textContent = "Asking Windows for this device's location…";
  setFormError("location-error", "");
  try {
    const result = await bridge.detect_location();
    if (result && result.ok) locationSaved(result);
    else setFormError("location-error", (result && result.message) || "The location was not found.");
  } finally {
    button.disabled = false;
    button.textContent = label;
  }
}

async function searchLocation(event) {
  event.preventDefault();
  const bridge = api();
  if (!bridge) return;
  const button = $("location-search");
  const list = $("location-results");
  button.disabled = true;
  setFormError("location-error", "");
  try {
    const result = await bridge.search_location($("location-query").value);
    list.replaceChildren();
    if (!result || !result.ok) {
      list.hidden = true;
      setFormError("location-error", (result && result.message) || "No place was found.");
      return;
    }
    result.places.forEach((place) => {
      const option = document.createElement("button");
      option.type = "button";
      option.setAttribute("role", "option");
      const name = document.createElement("strong");
      name.textContent = place.name;
      const detail = document.createElement("span");
      detail.textContent = `${Number(place.latitude).toFixed(3)}, ${Number(place.longitude).toFixed(3)}${place.timezone ? ` · ${place.timezone}` : ""}`;
      option.append(name, detail);
      option.addEventListener("click", () => saveLocation(place));
      list.appendChild(option);
    });
    list.hidden = false;
  } finally {
    button.disabled = false;
  }
}

async function saveLocation(place) {
  const bridge = api();
  if (!bridge) return;
  setFormError("location-error", "");
  const result = await bridge.save_location(place);
  if (result && result.ok) locationSaved(result);
  else setFormError("location-error", (result && result.message) || "The location was not saved.");
}

function bindHistory() {
  readSavedFilters();
  syncSegments();
  document.querySelectorAll(".view-tab").forEach((tab) => {
    tab.addEventListener("click", () => setView(tab.dataset.view));
  });
  $("history-miner").addEventListener("change", (event) => setHistoryFilter({ ip: event.target.value }));
  document.querySelectorAll("#history-period button").forEach((button) => {
    button.addEventListener("click", () => setHistoryFilter({ period: button.dataset.period }));
  });
  document.querySelectorAll("#history-metric button").forEach((button) => {
    button.addEventListener("click", () => setHistoryFilter({ metric: button.dataset.metric }));
  });
  $("history-location").addEventListener("click", openLocation);
  $("weather-chip").addEventListener("click", openLocation);
  $("location-detect").addEventListener("click", detectLocation);
  $("location-form").addEventListener("submit", searchLocation);
  $("history-view").addEventListener("scroll", hideTip, { passive: true });
  window.addEventListener("resize", () => {
    clearTimeout(historyState.resizeTimer);
    historyState.resizeTimer = setTimeout(() => {
      if (historyState.view === "history" && historyState.data) renderHistory(historyState.data);
    }, 150);
  });
}

bindHistory();
