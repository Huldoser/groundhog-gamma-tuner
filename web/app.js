const COLUMNS = ["name", "freq", "asic", "hash", "watts", "best", "shares", "up", "phase", "setpoint"];
const TUNER_FIELDS = [
  "min_freq", "start_freq", "max_freq",
  "min_volt", "start_volt", "max_volt",
  "max_temp", "max_watts", "max_vr_temp", "min_input_voltage", "max_error_percentage",
  "max_droop_mv",
];
const GLOBAL_FIELDS = [
  "voltage_step", "frequency_step", "monitor_interval", "refresh_interval",
  "default_target_temp", "temp_tolerance", "vr_temp_tolerance", "ceiling_soak_seconds",
  "flatline_hashrate_repeat_count",
];
const FALLBACK_PROMPT = "Set every miner to the Gamma 601 stock clocks (525 MHz / 1150 mV) and forget the saved setpoint?\n\nThe next Start Autotuner will climb or step down from there.";

const $ = (id) => document.getElementById(id);

let logCursor = 0;
let selectedIp = "";
let editIp = "";
let scanWasRunning = false;
let fullscreen = false;
let polling = false;
let confirmResolver = null;
let pollStarted = false;
let tunerRows = [];
let tunerIndex = -1;
let tunerClipboard = null;
let lastSnapshot = {
  miners: [],
  updated: "--:--:--",
  prompts: { baseline: FALLBACK_PROMPT },
  controls: {
    status: "idle",
    status_label: "Idle",
    reset_enabled: true,
    restart_all_enabled: true,
    scan_enabled: true,
  },
};

function api() {
  if (!window.pywebview || !window.pywebview.api) return null;
  return window.pywebview.api;
}

function openModal(id) {
  const modal = $(id);
  modal.hidden = false;
  const focus = modal.querySelector("[data-autofocus]");
  if (focus) focus.focus();
}

function closeModal(id) {
  $(id).hidden = true;
  clearSelection();
}

function setFormError(id, message) {
  $(id).textContent = message || "";
}

function showNotice(notice) {
  if (!notice || !notice.message) return;
  $("notice-title").textContent = notice.title || "Notice";
  $("notice-message").textContent = notice.message;
  openModal("notice");
}

function confirmAction({ title, message, confirmLabel, danger }) {
  if (confirmResolver) confirmResolver(false);
  return new Promise((resolve) => {
    confirmResolver = resolve;
    $("confirm-title").textContent = title;
    $("confirm-message").textContent = message;
    const ok = $("confirm-ok");
    ok.textContent = confirmLabel || "Confirm";
    ok.className = danger ? "danger" : "accent";
    openModal("confirm");
    ok.focus();
  });
}

function settleConfirm(value) {
  closeModal("confirm");
  const resolve = confirmResolver;
  confirmResolver = null;
  if (resolve) resolve(value);
}

function selectedMiner() {
  return (lastSnapshot.miners || []).find((miner) => miner.ip === selectedIp) || null;
}

function clearSelection() {
  if (!selectedIp) return;
  selectedIp = "";
  renderTable(lastSnapshot.miners || []);
}

function hideMenu() {
  $("row-menu").hidden = true;
  document.removeEventListener("pointerdown", hideMenuOnAway, true);
}

function hideMenuOnAway(event) {
  if (event.target.closest("#row-menu")) return;
  hideMenu();
}

function hideSettingsMenu() {
  $("settings-menu").hidden = true;
  $("settings-open").setAttribute("aria-expanded", "false");
  document.removeEventListener("pointerdown", hideSettingsOnAway, true);
}

function hideSettingsOnAway(event) {
  if (event.target.closest("#settings-menu") || event.target.closest("#settings-open")) return;
  hideSettingsMenu();
}

function showSettingsMenu() {
  hideMenu();
  const menu = $("settings-menu");
  const button = $("settings-open");
  menu.hidden = false;
  const buttonRect = button.getBoundingClientRect();
  const menuRect = menu.getBoundingClientRect();
  const left = Math.min(buttonRect.right - menuRect.width, window.innerWidth - menuRect.width - 8);
  const top = Math.min(buttonRect.bottom + 6, window.innerHeight - menuRect.height - 8);
  menu.style.left = `${Math.max(8, left)}px`;
  menu.style.top = `${Math.max(8, top)}px`;
  button.setAttribute("aria-expanded", "true");
  setTimeout(() => {
    document.removeEventListener("pointerdown", hideSettingsOnAway, true);
    document.addEventListener("pointerdown", hideSettingsOnAway, true);
  }, 0);
}

function toggleSettingsMenu() {
  if ($("settings-menu").hidden) showSettingsMenu();
  else hideSettingsMenu();
}

let runBusy = false;

function applyControls(controls) {
  controls = controls || {};
  const status = controls.status || "idle";
  const pill = $("status-pill");
  pill.textContent = controls.status_label || "Idle";
  pill.className = `pill ${status}`;
  $("updated").textContent = `Updated ${lastSnapshot.updated || "--:--:--"}`;
  const run = $("run");
  if (status === "running" || status === "stopping") {
    run.textContent = status === "stopping" ? "Stopping…" : "Stop Autotuner";
    run.className = status === "stopping" ? "danger is-busy" : "danger";
    run.dataset.action = "stop";
  } else {
    run.textContent = "Start Autotuner";
    run.className = "accent";
    run.dataset.action = "start";
  }
  const actionable = status === "idle" || status === "running";
  run.disabled = runBusy || !actionable;
  if (runBusy) run.classList.add("is-busy");
  const busy = runBusy || status === "stopping";
  run.setAttribute("aria-busy", busy ? "true" : "false");
  $("reset").disabled = !controls.reset_enabled;
  $("restart-all").disabled = !controls.restart_all_enabled;
  $("scan-open").disabled = !controls.scan_enabled;
  if ($("empty-scan")) $("empty-scan").disabled = !controls.scan_enabled;
}

let tableKey = "";
let sortColumn = "best";
let sortDirection = "desc";

const NUMERIC_COLUMNS = new Set(["freq", "asic", "hash", "watts", "best", "shares", "up", "setpoint"]);
const DIFFICULTY_SUFFIX = { k: 1e3, M: 1e6, G: 1e9, T: 1e12, P: 1e15 };

function plainNumber(text) {
  if (text == null) return null;
  const trimmed = String(text).trim().replace(/%$/, "");
  if (!trimmed || trimmed === "-") return null;
  const value = Number(trimmed);
  return Number.isFinite(value) ? value : null;
}

function difficultyNumber(miner, column) {
  const exact = plainNumber(miner[`${column}_title`]);
  if (exact != null) return exact;
  const match = String(miner[column] ?? "").trim().match(/^(-?\d+(?:\.\d+)?)([kMGTP])?$/);
  if (!match) return null;
  return Number(match[1]) * (DIFFICULTY_SUFFIX[match[2]] || 1);
}

function shareNumber(text) {
  const match = String(text ?? "").trim().match(/^(-?\d+)\s*\/\s*(-?\d+)$/);
  if (!match) return null;
  return Number(match[1]) * 1e9 + Number(match[2]);
}

function sortValue(miner, column) {
  if (column === "best" || column === "session") return difficultyNumber(miner, column);
  if (column === "shares") return shareNumber(miner.shares);
  if (column === "up") return plainNumber(miner.up_seconds);
  if (column === "setpoint") return plainNumber(miner.setpoint_freq);
  if (NUMERIC_COLUMNS.has(column)) return plainNumber(miner[column]);
  const text = String(miner[column] ?? "").trim();
  if (!text || text === "-") return null;
  return text.toLowerCase();
}

function compareMiners(a, b) {
  const av = sortValue(a, sortColumn);
  const bv = sortValue(b, sortColumn);
  if (av == null && bv == null) return 0;
  if (av == null) return 1;
  if (bv == null) return -1;
  let result = 0;
  if (typeof av === "number" && typeof bv === "number") result = av - bv;
  else result = String(av).localeCompare(String(bv), undefined, { numeric: true, sensitivity: "base" });
  return sortDirection === "asc" ? result : -result;
}

function syncSortHeaders() {
  document.querySelectorAll("[data-sort]").forEach((button) => {
    const column = button.getAttribute("data-sort");
    const header = button.closest("th");
    if (!header) return;
    if (column !== sortColumn) header.setAttribute("aria-sort", "none");
    else header.setAttribute("aria-sort", sortDirection === "asc" ? "ascending" : "descending");
  });
}

function shown(value) {
  return value != null && String(value).trim() !== "" && String(value).trim() !== "-";
}

function addLine(cell, text, className) {
  const line = document.createElement("span");
  line.className = className ? `line ${className}` : "line";
  line.textContent = text;
  cell.appendChild(line);
}

function addNote(cell, text, note, className, noteClass) {
  const line = document.createElement("span");
  line.className = className ? `line ${className}` : "line";
  line.append(document.createTextNode(`${text} `));
  const label = document.createElement("span");
  label.className = noteClass ? `note ${noteClass}` : "note";
  label.textContent = note;
  line.append(label);
  cell.appendChild(line);
}

function setTitle(cell, title) {
  if (title) cell.title = title;
}

function firmwareTitle(miner) {
  const running = String(miner.name_title || "").trim();
  if (!shown(miner.firmware_update)) return running;
  const available = `Stable firmware ${miner.firmware_update} is available`;
  return running ? `${running}\n${available}` : available;
}

function levelClass(level, quiet) {
  if (level === "warn" || level === "bad") return level;
  return quiet ? "muted" : "";
}

const PHASE_TAGS = new Set(["hold", "climb", "trim", "alert"]);

function renderCell(miner, column) {
  const cell = document.createElement("td");
  if (column === "name") {
    cell.className = "left";
    if (!shown(miner.name) && !shown(miner.ip)) cell.textContent = "-";
    else {
      const update = shown(miner.firmware_update) ? miner.firmware_update : "";
      if (shown(miner.name) && miner.wifi_weak && shown(miner.wifi)) {
        addNote(cell, miner.name, `${miner.wifi} dBm`, "", "bad");
        if (update) addLine(cell, update, "warn");
      } else if (shown(miner.name) && update) {
        addNote(cell, miner.name, update, "", "warn");
      } else if (shown(miner.name)) addLine(cell, miner.name);
      else if (update) addLine(cell, update, "warn");
      if (shown(miner.ip)) addLine(cell, miner.ip, "muted");
      if (shown(miner.pool)) {
        addLine(cell, miner.fallback ? `${miner.pool} fallback` : miner.pool, "muted");
      }
    }
    setTitle(cell, firmwareTitle(miner));
    return cell;
  }
  if (column === "freq") {
    if (!shown(miner.freq) && !shown(miner.mv)) cell.textContent = "-";
    else {
      if (shown(miner.freq)) addLine(cell, `${miner.freq} MHz`);
      if (shown(miner.mv)) addLine(cell, `${miner.mv} mV`, miner.mv_alert ? "droop" : "muted");
    }
    setTitle(cell, miner.mv_title);
    return cell;
  }
  if (column === "asic") {
    if (!shown(miner.asic) && !shown(miner.vr)) cell.textContent = "-";
    else {
      if (shown(miner.asic)) addNote(cell, `${miner.asic}°C`, "asic", levelClass(miner.asic_level, false));
      if (shown(miner.vr)) addNote(cell, `${miner.vr}°C`, "vr", levelClass(miner.vr_level, true));
    }
    return cell;
  }
  if (column === "hash") {
    if (!shown(miner.hash) && !shown(miner.jth)) cell.textContent = "-";
    else {
      if (shown(miner.hash)) addLine(cell, miner.hash_label || `${miner.hash} GH/s`);
      if (shown(miner.jth)) addLine(cell, `${miner.jth} J/TH`, "muted");
    }
    setTitle(cell, miner.hash_title);
    return cell;
  }
  if (column === "watts") {
    if (!shown(miner.watts) && !shown(miner.vin)) cell.textContent = "-";
    else {
      if (shown(miner.watts)) addLine(cell, `${miner.watts} W`, miner.watts_alert ? "bad" : "");
      if (shown(miner.vin)) addLine(cell, `${miner.vin} V`, miner.vin_alert ? "bad" : "muted");
    }
    return cell;
  }
  if (column === "best") {
    if (!shown(miner.best) && !shown(miner.session)) cell.textContent = "-";
    else {
      if (shown(miner.best)) addLine(cell, miner.best);
      if (shown(miner.session)) addNote(cell, miner.session, "session", "muted");
    }
    const titles = [];
    if (miner.best_title) titles.push(miner.best_title);
    if (miner.session_title) titles.push(`Session ${miner.session_title}`);
    setTitle(cell, titles.join("\n"));
    return cell;
  }
  if (column === "shares") {
    if (!shown(miner.shares) && !shown(miner.error)) cell.textContent = "-";
    else {
      if (shown(miner.shares)) addLine(cell, miner.shares);
      if (shown(miner.error)) addLine(cell, miner.error, miner.error_alert ? "bad" : "muted");
    }
    setTitle(cell, miner.shares_title);
    return cell;
  }
  if (column === "phase") {
    cell.className = "phase";
    const value = miner.phase;
    if (!shown(value)) {
      cell.textContent = "-";
      return cell;
    }
    const pill = document.createElement("span");
    const tag = PHASE_TAGS.has(miner.tag) ? miner.tag : "idle";
    pill.className = `phase-pill ${tag}`;
    pill.textContent = value;
    cell.appendChild(pill);
    if (shown(miner.reason)) addLine(cell, miner.reason, "muted reason");
    return cell;
  }
  if (column === "setpoint") {
    cell.className = "left";
    if (!shown(miner.setpoint_freq) && !shown(miner.setpoint_volt)) {
      cell.textContent = "-";
      return cell;
    }
    if (shown(miner.setpoint_freq)) addLine(cell, `${miner.setpoint_freq} MHz`);
    if (shown(miner.setpoint_volt)) {
      const limit = shown(miner.setpoint_limit) ? ` \u00b7 ${miner.setpoint_limit}` : "";
      addLine(cell, `${miner.setpoint_volt} mV${limit}`, "muted");
    }
    return cell;
  }
  if (column === "up") cell.className = "quiet";
  const value = miner[column];
  cell.textContent = value == null || value === "" ? "-" : value;
  return cell;
}

function renderTable(miners) {
  const rows = miners.slice().sort(compareMiners);
  const key = `${selectedIp}|${sortColumn}|${sortDirection}|${JSON.stringify(rows)}`;
  syncSortHeaders();
  if (key === tableKey) {
    $("empty").hidden = miners.length > 0;
    return;
  }
  tableKey = key;
  const body = $("miner-body");
  const scroll = $("table-scroll");
  const top = scroll.scrollTop;
  body.replaceChildren();
  rows.forEach((miner) => {
    const tr = document.createElement("tr");
    tr.className = `tag-${miner.tag || "idle"}`;
    if (miner.ip === selectedIp) tr.classList.add("selected");
    tr.dataset.ip = miner.ip;
    COLUMNS.forEach((column) => {
      tr.appendChild(renderCell(miner, column));
    });
    body.appendChild(tr);
  });
  scroll.scrollTop = top;
  $("empty").hidden = miners.length > 0;
  if (selectedIp && !miners.some((row) => row.ip === selectedIp)) selectedIp = "";
}

function appendLog(lines) {
  if (!lines || !lines.length) return;
  const log = $("log");
  const stick = log.scrollHeight - log.scrollTop - log.clientHeight < 48;
  lines.forEach((line) => {
    const row = document.createElement("div");
    row.className = `log-line ${line.level || "info"}`;
    row.textContent = line.text;
    log.appendChild(row);
  });
  while (log.children.length > 500) log.removeChild(log.firstChild);
  if (stick) log.scrollTop = log.scrollHeight;
  logCursor = lines[lines.length - 1].id;
}

function syncScan(scan) {
  const running = Boolean(scan && scan.running);
  if (running) {
    scanWasRunning = true;
    $("scan-progress").textContent = scan.message || "";
    $("scan-start").disabled = true;
    $("scan-start-ip").disabled = true;
    $("scan-end-ip").disabled = true;
    return;
  }
  if (!scanWasRunning) return;
  scanWasRunning = false;
  $("scan-start").disabled = false;
  $("scan-start-ip").disabled = false;
  $("scan-end-ip").disabled = false;
  $("scan-progress").textContent = "";
  closeModal("scan");
}

function renderNetwork(network) {
  if (!network) return;
  const diff = $("network-diff");
  diff.textContent = network.difficulty || "--";
  const label = diff.parentElement;
  const exact = network.difficulty_title || "";
  label.title = exact
    ? `Difficulty a share must beat to find a block\n${exact}`
    : "Difficulty a share must beat to find a block";
  const byName = {};
  (network.pools || []).forEach((pool) => {
    byName[pool.name] = pool;
  });
  document.querySelectorAll(".pool").forEach((item) => {
    const pool = byName[item.dataset.pool];
    if (!pool) return;
    const state = pool.online === true ? "online" : pool.online === false ? "offline" : "unknown";
    item.className = `pool ${state}`;
    item.title = state === "online" ? "Online" : state === "offline" ? "Offline" : "Checking";
  });
}

function addStat(parent, label, value, tone, title, unit) {
  const item = document.createElement("span");
  item.className = tone ? `stat ${tone}` : "stat";
  if (title) item.title = title;
  if (shown(label)) {
    const name = document.createElement("span");
    name.className = "stat-label";
    name.textContent = label;
    item.appendChild(name);
  }
  const figure = document.createElement("strong");
  figure.textContent = value;
  item.appendChild(figure);
  if (shown(unit)) item.append(document.createTextNode(unit));
  parent.appendChild(item);
}

function renderFleet(fleet, miners) {
  const strip = $("summary");
  const fleetEl = $("fleet");
  if (!fleetEl) return;
  if (!miners || !miners.length || !fleet) {
    if (strip) strip.hidden = true;
    fleetEl.hidden = true;
    fleetEl.replaceChildren();
    return;
  }
  fleetEl.replaceChildren();
  addStat(fleetEl, "Online", String(fleet.online));
  if (fleet.offline) addStat(fleetEl, "Offline", String(fleet.offline), "offline");
  if (shown(fleet.hash)) addStat(fleetEl, "Hash", fleet.hash);
  if (shown(fleet.watts)) addStat(fleetEl, "Power", `${fleet.watts} W`);
  if (shown(fleet.jth)) addStat(fleetEl, "", fleet.jth, "", "", "J/TH");
  if (fleet.hold) addStat(fleetEl, "Holding", String(fleet.hold), "hold");
  if (fleet.trim) addStat(fleetEl, "Trimming", String(fleet.trim), "trim");
  fleetEl.hidden = false;
  if (strip) strip.hidden = false;
}

let configErrorShown = false;

function applySnapshot(snapshot) {
  lastSnapshot = snapshot;
  if (snapshot.config_error && !configErrorShown) {
    configErrorShown = true;
    showNotice({
      title: "Config file damaged",
      message: snapshot.config_error,
      level: "error",
    });
  }
  applyControls(snapshot.controls || lastSnapshot.controls);
  if (selectedIp && !(snapshot.miners || []).some((miner) => miner.ip === selectedIp)) {
    selectedIp = "";
  }
  renderTable(snapshot.miners || []);
  appendLog(snapshot.log || []);
  syncScan(snapshot.scan);
  renderNetwork(snapshot.network);
  renderFleet(snapshot.fleet, snapshot.miners || []);
}

function windowFocused() {
  return document.visibilityState === "visible" && document.hasFocus();
}

async function poll() {
  const bridge = api();
  if (!bridge || polling) return;
  polling = true;
  try {
    const snapshot = await bridge.get_snapshot(logCursor, windowFocused());
    if (snapshot && snapshot.miners) applySnapshot(snapshot);
  } catch (_error) {
    // Keep the last good screen. The next poll tries again.
  } finally {
    polling = false;
  }
}

function requireSelection(message) {
  const miner = selectedMiner();
  if (miner) return miner;
  showNotice({ title: "No Selection", message: message || "Please select a miner first.", level: "warning" });
  return null;
}

async function onRun() {
  const bridge = api();
  const run = $("run");
  if (!bridge || runBusy || run.disabled) return;
  const action = run.dataset.action || "start";
  runBusy = true;
  applyControls(lastSnapshot.controls);
  try {
    if (action === "stop") {
      await bridge.stop_autotuner();
    } else {
      const result = await bridge.start_autotuner();
      if (result && result.notice) showNotice(result.notice);
    }
    const snapshot = await bridge.get_snapshot(logCursor, windowFocused());
    if (snapshot && snapshot.miners) applySnapshot(snapshot);
  } catch (_error) {
    // The next poll reconciles the button with the tuner.
  } finally {
    runBusy = false;
    applyControls(lastSnapshot.controls);
  }
}

async function restartAll() {
  const bridge = api();
  if (!bridge) return;
  const yes = await confirmAction({
    title: "Restart All Miners",
    message: "Restart every saved miner?",
    confirmLabel: "Restart",
    danger: true,
  });
  if (!yes) return;
  const result = await bridge.restart_all_miners();
  if (result && result.notice) showNotice(result.notice);
  poll();
}

async function onReset() {
  const bridge = api();
  if (!bridge) return;
  const prompt = (lastSnapshot.prompts && lastSnapshot.prompts.baseline) || FALLBACK_PROMPT;
  const yes = await confirmAction({
    title: "Reset All to Baseline",
    message: prompt,
    confirmLabel: "Reset",
    danger: true,
  });
  if (!yes) return;
  const result = await bridge.reset_baseline();
  if (result && result.notice) showNotice(result.notice);
  poll();
}

function openScan() {
  setFormError("scan-error", "");
  if (!scanWasRunning) $("scan-progress").textContent = "";
  const range = lastSnapshot.scan_range || {};
  if (!$("scan-start-ip").value.trim() && range.start) $("scan-start-ip").value = range.start;
  if (!$("scan-end-ip").value.trim() && range.end) $("scan-end-ip").value = range.end;
  openModal("scan");
}

async function submitScan(event) {
  event.preventDefault();
  const bridge = api();
  if (!bridge) return;
  const button = $("scan-start");
  button.disabled = true;
  setFormError("scan-error", "");
  try {
    const result = await bridge.start_scan($("scan-start-ip").value, $("scan-end-ip").value);
    if (!result.ok) {
      setFormError("scan-error", result.message);
      button.disabled = false;
      return;
    }
  } catch (_error) {
    button.disabled = false;
    return;
  }
  scanWasRunning = true;
  $("scan-progress").textContent = "Starting scan...";
  $("scan-start-ip").disabled = true;
  $("scan-end-ip").disabled = true;
  poll();
}

async function closeScan() {
  const bridge = api();
  if (!bridge) {
    closeModal("scan");
    return;
  }
  const result = await bridge.cancel_scan();
  if (!result.running) closeModal("scan");
  poll();
}

async function removeSelected() {
  hideMenu();
  const miner = requireSelection("Please select a miner to delete.");
  const bridge = api();
  if (!miner || !bridge) return;
  const yes = await confirmAction({
    title: "Delete Miner",
    message: `Remove ${miner.name} (${miner.ip})?`,
    confirmLabel: "Remove",
    danger: true,
  });
  if (!yes) return;
  const result = await bridge.remove_miner_address(miner.ip);
  if (result && !result.ok && result.notice) showNotice(result.notice);
  if (result && result.ok) selectedIp = "";
  poll();
}

function openEdit() {
  const miner = requireSelection("Please select a miner first.");
  if (!miner) return;
  editIp = miner.ip;
  $("edit-name").value = miner.name === "-" ? "" : miner.name;
  $("edit-ip").value = miner.ip;
  setFormError("edit-error", "");
  $("edit-submit").disabled = false;
  $("edit-submit").textContent = "Save";
  hideMenu();
  openModal("edit");
}

async function submitEdit(event) {
  event.preventDefault();
  const bridge = api();
  if (!bridge) return;
  const button = $("edit-submit");
  const newIp = $("edit-ip").value.trim();
  button.disabled = true;
  button.textContent = newIp !== editIp ? "Checking board..." : "Save";
  setFormError("edit-error", "");
  try {
    const result = await bridge.edit_miner(editIp, $("edit-name").value, newIp);
    if (!result.ok) {
      setFormError("edit-error", result.message);
      return;
    }
    closeModal("edit");
    poll();
  } finally {
    button.disabled = false;
    button.textContent = "Save";
  }
}

async function restartSelected() {
  hideMenu();
  const miner = requireSelection("Please select a miner first.");
  const bridge = api();
  if (!miner || !bridge) return;
  const yes = await confirmAction({
    title: "Restart Miner",
    message: `Restart ${miner.name} (${miner.ip})?`,
    confirmLabel: "Restart",
    danger: true,
  });
  if (!yes) return;
  const result = await bridge.restart_miner(miner.ip);
  if (result && result.notice) showNotice(result.notice);
  poll();
}

async function openGlobal() {
  const bridge = api();
  if (!bridge) return;
  setFormError("global-error", "");
  const result = await bridge.get_global_settings();
  if (!result.ok) {
    showNotice(result.notice || { title: "Error", message: result.message });
    return;
  }
  const settings = result.settings || {};
  GLOBAL_FIELDS.forEach((key) => {
    $(key).value = settings[key] == null ? "" : settings[key];
  });
  $("flatline_detection_enabled").checked = Boolean(settings.flatline_detection_enabled);
  openModal("global");
  $("voltage_step").focus();
}

async function submitGlobal(event) {
  event.preventDefault();
  const bridge = api();
  if (!bridge) return;
  const settings = {};
  GLOBAL_FIELDS.forEach((key) => {
    settings[key] = $(key).value.trim();
  });
  settings.flatline_detection_enabled = $("flatline_detection_enabled").checked;
  const result = await bridge.save_global_settings(settings);
  if (!result.ok) {
    setFormError("global-error", result.message);
    return;
  }
  closeModal("global");
  poll();
}

function tunerField(name) {
  return $(`tune-${name}`);
}

function storeTunerForm() {
  if (tunerIndex < 0 || !tunerRows[tunerIndex]) return;
  const row = tunerRows[tunerIndex];
  const box = $("tune-enabled");
  row.enabled = box.checked && !box.disabled;
  TUNER_FIELDS.forEach((field) => {
    row.fields[field] = tunerField(field).value.trim();
  });
}

function validateTuner() {
  const empty = TUNER_FIELDS.some((field) => tunerField(field).value.trim() === "");
  const box = $("tune-enabled");
  if (empty) {
    box.checked = false;
    box.disabled = true;
  } else {
    box.disabled = false;
  }
}

function showTuner(index) {
  tunerIndex = index;
  const row = tunerRows[index];
  $("tuner-title").textContent = row.label;
  $("tune-enabled").checked = Boolean(row.enabled);
  TUNER_FIELDS.forEach((field) => {
    tunerField(field).value = row.fields[field] == null ? "" : row.fields[field];
  });
  validateTuner();
  Array.from($("tuner-list").children).forEach((button, buttonIndex) => {
    button.classList.toggle("selected", buttonIndex === index);
  });
}

function renderTunerList() {
  const list = $("tuner-list");
  list.replaceChildren();
  tunerRows.forEach((row, index) => {
    const button = document.createElement("button");
    button.type = "button";
    const name = document.createElement("span");
    name.className = "tuner-name";
    name.textContent = row.name || row.ip;
    button.append(name);
    if (row.ip && row.ip !== row.name) {
      const address = document.createElement("span");
      address.className = "tuner-ip";
      address.textContent = row.ip;
      button.append(address);
    }
    button.addEventListener("click", () => {
      if (index === tunerIndex) return;
      storeTunerForm();
      showTuner(index);
    });
    list.appendChild(button);
  });
}

async function openTuner() {
  const bridge = api();
  if (!bridge) return;
  setFormError("tuner-error", "");
  const result = await bridge.get_autotuner_settings();
  if (!result.ok) {
    showNotice(result.notice || { title: "No Miners Found", message: result.message, level: "warning" });
    return;
  }
  tunerRows = (result.miners || []).map((miner) => ({
    ip: miner.ip,
    name: miner.name || miner.label || miner.ip,
    label: miner.label,
    enabled: Boolean(miner.enabled),
    fields: Object.assign({}, miner.fields),
  }));
  tunerIndex = -1;
  renderTunerList();
  openModal("tuner");
  if (tunerRows.length) showTuner(0);
}

function copyTuner() {
  tunerClipboard = {};
  TUNER_FIELDS.forEach((field) => {
    tunerClipboard[field] = tunerField(field).value;
  });
}

function pasteTuner() {
  if (!tunerClipboard) {
    showNotice({ title: "No Data", message: "No row has been copied yet.", level: "warning" });
    return;
  }
  TUNER_FIELDS.forEach((field) => {
    if (field in tunerClipboard) tunerField(field).value = tunerClipboard[field];
  });
  validateTuner();
  storeTunerForm();
}

async function submitTuner(event) {
  event.preventDefault();
  const bridge = api();
  if (!bridge) return;
  storeTunerForm();
  const result = await bridge.save_autotuner_settings(tunerRows);
  if (!result.ok) {
    setFormError("tuner-error", result.message);
    return;
  }
  closeModal("tuner");
  poll();
}

async function setFullscreen(enabled) {
  const bridge = api();
  fullscreen = Boolean(enabled);
  if (bridge) {
    const result = await bridge.set_fullscreen(fullscreen);
    if (result && result.ok === false) fullscreen = !fullscreen;
    else if (result && typeof result.fullscreen === "boolean") fullscreen = result.fullscreen;
  }
  const label = fullscreen ? "Exit fullscreen" : "Fullscreen";
  const button = $("fullscreen");
  button.setAttribute("aria-label", label);
  button.title = label;
  button.querySelector(".icon-expand").hidden = fullscreen;
  button.querySelector(".icon-compress").hidden = !fullscreen;
  document.body.classList.toggle("fullscreen", fullscreen);
}

function showRowMenu(event) {
  hideSettingsMenu();
  const menu = $("row-menu");
  menu.hidden = false;
  const rect = menu.getBoundingClientRect();
  const left = Math.min(event.clientX, window.innerWidth - rect.width - 8);
  const top = Math.min(event.clientY, window.innerHeight - rect.height - 8);
  menu.style.left = `${Math.max(8, left)}px`;
  menu.style.top = `${Math.max(8, top)}px`;
  setTimeout(() => {
    document.removeEventListener("pointerdown", hideMenuOnAway, true);
    document.addEventListener("pointerdown", hideMenuOnAway, true);
  }, 0);
}

function bind() {
  $("fullscreen").addEventListener("click", () => setFullscreen(!fullscreen));
  $("settings-open").addEventListener("click", toggleSettingsMenu);
  $("scan-open").addEventListener("click", openScan);
  $("empty-scan").addEventListener("click", openScan);
  $("run").addEventListener("click", onRun);
  $("scan-form").addEventListener("submit", submitScan);
  $("scan-cancel").addEventListener("click", closeScan);
  $("edit-form").addEventListener("submit", submitEdit);
  $("global-form").addEventListener("submit", submitGlobal);
  $("tuner-form").addEventListener("submit", submitTuner);
  $("tuner-copy").addEventListener("click", copyTuner);
  $("tuner-paste").addEventListener("click", pasteTuner);
  TUNER_FIELDS.forEach((field) => {
    tunerField(field).addEventListener("input", validateTuner);
  });
  $("confirm-ok").addEventListener("click", () => settleConfirm(true));
  $("confirm-cancel").addEventListener("click", () => settleConfirm(false));
  $("notice-ok").addEventListener("click", () => closeModal("notice"));

  document.querySelectorAll("[data-close]").forEach((element) => {
    element.addEventListener("click", () => {
      const id = element.getAttribute("data-close");
      if (id === "scan") closeScan();
      else if (id === "confirm") settleConfirm(false);
      else closeModal(id);
    });
  });

  $("settings-menu").addEventListener("click", (event) => {
    const button = event.target.closest("button");
    if (!button || button.disabled) return;
    const action = button.getAttribute("data-settings");
    if (!action) return;
    hideSettingsMenu();
    if (action === "global") openGlobal();
    if (action === "tuner") openTuner();
    if (action === "restart-all") restartAll();
    if (action === "reset") onReset();
  });

  $("row-menu").addEventListener("click", (event) => {
    const action = event.target.getAttribute("data-menu");
    if (action === "edit") openEdit();
    if (action === "restart") restartSelected();
    if (action === "remove") removeSelected();
  });

  $("miner-body").addEventListener("click", (event) => {
    const row = event.target.closest("tr");
    if (!row) return;
    selectedIp = row.dataset.ip;
    renderTable(lastSnapshot.miners || []);
  });
  document.addEventListener("pointerdown", (event) => {
    if (event.target.closest("#miner-body tr")) return;
    if (event.target.closest("#row-menu")) return;
    if (event.target.closest(".modal:not([hidden])")) return;
    clearSelection();
  }, true);
  $("miner-body").addEventListener("contextmenu", (event) => {
    const row = event.target.closest("tr");
    if (!row) return;
    event.preventDefault();
    selectedIp = row.dataset.ip;
    renderTable(lastSnapshot.miners || []);
    showRowMenu(event);
  });

  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") {
      if (!$("settings-menu").hidden) {
        hideSettingsMenu();
        return;
      }
      if (!$("row-menu").hidden) {
        hideMenu();
        clearSelection();
        return;
      }
      if (!$("notice").hidden) {
        closeModal("notice");
        return;
      }
      if (!$("confirm").hidden) {
        settleConfirm(false);
        return;
      }
      const open = ["tuner", "global", "edit", "scan"].find((id) => !$(id).hidden);
      if (open === "scan") {
        closeScan();
        return;
      }
      if (open) {
        closeModal(open);
        return;
      }
      if (fullscreen) setFullscreen(false);
      return;
    }
    if (event.key === "F11") {
      event.preventDefault();
      setFullscreen(!fullscreen);
    }
  });
  window.addEventListener("resize", () => {
    hideMenu();
    hideSettingsMenu();
  });
  document.querySelector(".table-card").addEventListener("scroll", () => {
    hideMenu();
    hideSettingsMenu();
  }, true);
  document.querySelectorAll("[data-sort]").forEach((button) => {
    button.addEventListener("click", () => {
      const column = button.getAttribute("data-sort");
      if (sortColumn === column) {
        sortDirection = sortDirection === "asc" ? "desc" : "asc";
      } else {
        sortColumn = column;
        sortDirection = NUMERIC_COLUMNS.has(column) ? "desc" : "asc";
      }
      renderTable(lastSnapshot.miners || []);
    });
  });
}

function startPolling() {
  if (pollStarted) return;
  pollStarted = true;
  poll();
  setInterval(poll, 500);
}

bind();
if (api()) startPolling();
else window.addEventListener("pywebviewready", startPolling);
