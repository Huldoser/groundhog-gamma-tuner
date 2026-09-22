const COLUMNS = ["name", "freq", "asic", "hash", "watts", "best", "shares", "up", "phase", "setpoint"];
const TUNER_FIELDS = [
  "min_freq", "start_freq", "max_freq",
  "min_volt", "start_volt", "max_volt",
  "max_temp", "max_watts", "max_vr_temp", "min_input_voltage", "max_error_percentage",
];
const GLOBAL_FIELDS = [
  "voltage_step", "frequency_step", "monitor_interval", "refresh_interval",
  "default_target_temp", "temp_tolerance", "flatline_hashrate_repeat_count",
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
    start_enabled: true,
    start_label: "Start Autotuner",
    stop_enabled: false,
    reset_enabled: true,
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

function applyControls(controls) {
  const pill = $("status-pill");
  pill.textContent = controls.status_label || "Idle";
  pill.className = `pill ${controls.status || "idle"}`;
  $("updated").textContent = `Updated ${lastSnapshot.updated || "--:--:--"}`;
  const start = $("start");
  start.disabled = !controls.start_enabled;
  start.textContent = controls.start_label || "Start Autotuner";
  start.classList.toggle("is-running", controls.start_label === "Autotuner Running");
  $("stop").disabled = !controls.stop_enabled;
  $("reset").disabled = !controls.reset_enabled;
  $("scan-open").disabled = !controls.scan_enabled;
}

let tableKey = "";
let sortColumn = "best";
let sortDirection = "desc";

const NUMERIC_COLUMNS = new Set(["freq", "asic", "hash", "watts", "best", "shares", "up"]);
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

function addNote(cell, text, note, className) {
  const line = document.createElement("span");
  line.className = className ? `line ${className}` : "line";
  line.append(document.createTextNode(`${text} `));
  const label = document.createElement("span");
  label.className = "note";
  label.textContent = note;
  line.append(label);
  cell.appendChild(line);
}

function setTitle(cell, title) {
  if (title) cell.title = title;
}

function renderCell(miner, column) {
  const cell = document.createElement("td");
  if (column === "name") {
    cell.className = "left";
    if (!shown(miner.name) && !shown(miner.ip)) cell.textContent = "-";
    else {
      if (shown(miner.name)) addLine(cell, miner.name);
      if (shown(miner.ip)) addLine(cell, miner.ip, "muted");
    }
    setTitle(cell, miner.name_title);
    return cell;
  }
  if (column === "freq") {
    if (!shown(miner.freq) && !shown(miner.mv)) cell.textContent = "-";
    else {
      if (shown(miner.freq)) addLine(cell, `${miner.freq} MHz`);
      if (shown(miner.mv)) addLine(cell, `${miner.mv} mV`, miner.mv_alert ? "droop" : "");
    }
    setTitle(cell, miner.mv_title);
    return cell;
  }
  if (column === "asic") {
    if (!shown(miner.asic) && !shown(miner.vr)) cell.textContent = "-";
    else {
      if (shown(miner.asic)) addNote(cell, `${miner.asic}°C`, "asic");
      if (shown(miner.vr)) addNote(cell, `${miner.vr}°C`, "vr");
    }
    return cell;
  }
  if (column === "hash") {
    if (!shown(miner.hash) && !shown(miner.jth)) cell.textContent = "-";
    else {
      if (shown(miner.hash)) addLine(cell, `${miner.hash} GH/s`);
      if (shown(miner.jth)) addLine(cell, `${miner.jth} J/TH`, "muted");
    }
    setTitle(cell, miner.hash_title);
    return cell;
  }
  if (column === "watts") {
    if (!shown(miner.watts) && !shown(miner.vin)) cell.textContent = "-";
    else {
      if (shown(miner.watts)) addLine(cell, `${miner.watts} W`);
      if (shown(miner.vin)) addLine(cell, `${miner.vin} V`, "muted");
    }
    return cell;
  }
  if (column === "best") {
    if (!shown(miner.best) && !shown(miner.session)) cell.textContent = "-";
    else {
      if (shown(miner.best)) addLine(cell, miner.best);
      if (shown(miner.session)) addNote(cell, miner.session, "session");
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
      if (shown(miner.error)) addLine(cell, miner.error, "muted");
    }
    setTitle(cell, miner.shares_title);
    return cell;
  }
  if (column === "setpoint") cell.className = "left";
  if (column === "phase") cell.className = "phase";
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

function applySnapshot(snapshot) {
  lastSnapshot = snapshot;
  applyControls(snapshot.controls || lastSnapshot.controls);
  if (selectedIp && !(snapshot.miners || []).some((miner) => miner.ip === selectedIp)) {
    selectedIp = "";
  }
  renderTable(snapshot.miners || []);
  appendLog(snapshot.log || []);
  syncScan(snapshot.scan);
  renderNetwork(snapshot.network);
}

async function poll() {
  const bridge = api();
  if (!bridge || polling) return;
  polling = true;
  try {
    const snapshot = await bridge.get_snapshot(logCursor);
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

async function onStart() {
  const bridge = api();
  if (!bridge) return;
  const result = await bridge.start_autotuner();
  if (result && result.notice) showNotice(result.notice);
  poll();
}

async function onStop() {
  const bridge = api();
  if (!bridge) return;
  await bridge.stop_autotuner();
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
    selectedIp = newIp;
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
  $("start").addEventListener("click", onStart);
  $("stop").addEventListener("click", onStop);
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
  $("miner-body").addEventListener("dblclick", (event) => {
    const row = event.target.closest("tr");
    if (!row) return;
    selectedIp = row.dataset.ip;
    renderTable(lastSnapshot.miners || []);
    openEdit();
  });
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
