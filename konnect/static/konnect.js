/* Tiny SPA for onboarding + dashboard. No framework — served from Flask. */
(() => {
  const $ = (sel) => document.querySelector(sel);
  const show = (id) => {
    document.querySelectorAll(".step").forEach((s) => (s.hidden = true));
    const el = document.getElementById(id);
    if (el) el.hidden = false;
  };

  // The onboarding SPA is always served from <base>/static/index.html.
  // The API roots live one directory up. Compute BASE_URL from where
  // we're loaded so it works both at :7130/ (BASE="/") and behind an
  // nginx proxy at /konnect/ (BASE="/konnect/").
  const BASE_URL = new URL("../", window.location.href).pathname;

  // Build a prefixed API path. Leading slash on `path` is ignored so
  // callers can write either `/connection` or `connection`.
  const api_url = (path) => BASE_URL + path.replace(/^\//, "");

  const api = async (method, path, body) => {
    const res = await fetch(api_url(path), {
      method,
      headers: body ? { "Content-Type": "application/json" } : undefined,
      body: body ? JSON.stringify(body) : undefined,
    });
    let data = null;
    try { data = await res.json(); } catch { /* no body */ }
    if (!res.ok) {
      const msg = data?.error || `${res.status} ${res.statusText}`;
      throw new Error(msg);
    }
    return data;
  };
  const flashError = (msg) => {
    const el = $("#error");
    el.textContent = msg;
    el.hidden = false;
    setTimeout(() => (el.hidden = true), 6000);
  };

  /* ---- state ---------------------------------------------------------- */
  const state = {
    selectedType: null,
    selectedCamera: null,
    printer: null,
    types: [],
    connection: null,
    pollTimer: null,
  };

  /* ---- step 1: printer type ------------------------------------------ */
  const renderTypes = (list, current) => {
    const container = $("#type-list");
    container.innerHTML = "";
    // Sort recommended picks first: I3MK3S, HT90, then the rest.
    const order = { I3MK3S: 0, HT90: 1 };
    list.sort((a, b) => (order[a.name] ?? 99) - (order[b.name] ?? 99));
    for (const t of list) {
      const card = document.createElement("div");
      card.className = "type-card";
      if (t.name === current) card.classList.add("selected");
      card.dataset.name = t.name;
      card.innerHTML = `
        <div class="name">${t.label}</div>
        <div class="desc">${t.description}</div>
        ${t.recommended_for
          ? `<div class="rec">Best for: ${t.recommended_for}</div>`
          : ""}
      `;
      card.addEventListener("click", () => {
        document.querySelectorAll(".type-card").forEach((c) =>
          c.classList.remove("selected"));
        card.classList.add("selected");
        state.selectedType = t.name;
        $("#type-next").disabled = false;
      });
      container.appendChild(card);
    }
    if (current) state.selectedType = current;
    $("#type-next").disabled = !state.selectedType;
  };

  /* ---- step 2: identity ---------------------------------------------- */
  const fillIdentity = () => {
    $("#sn").value = state.printer.serial_number || "";
    $("#name").value = state.printer.name || "";
    $("#location").value = state.printer.location || "";
  };

  /* ---- step 3: registration ------------------------------------------ */
  const drawQR = (text) => {
    // Server renders a PNG using Python's qrcode lib. Use api_url so
    // the /qr endpoint resolves correctly behind the nginx proxy.
    const img = $("#qr");
    img.src = api_url("qr?data=" + encodeURIComponent(text));
  };

  const pollConnection = () => {
    clearTimeout(state.pollTimer);
    state.pollTimer = setTimeout(async () => {
      try {
        const c = await api("GET", "/connection");
        state.connection = c;
        if (c.registration === "FINISHED") {
          $("#register-status").textContent = "Registered!";
          $("#register-status").classList.add("ok");
          setTimeout(() => gotoCamera(), 800);
          return;
        }
      } catch (e) {
        console.warn(e);
      }
      pollConnection();
    }, 2000);
  };

  const startRegister = async () => {
    try {
      const r = await api("POST", "/connection", {
        connect: {
          hostname: "connect.prusa3d.com",
          tls: true,
          port: 443,
        },
      });
      $("#code-box").textContent = r.code || "—";
      drawQR(r.url || "https://connect.prusa3d.com/add");
      $("#connect-link").href = r.url || "https://connect.prusa3d.com/add";
      $("#register-result").hidden = false;
      $("#register-go").hidden = true;
      pollConnection();
    } catch (e) {
      flashError(e.message);
    }
  };

  const cancelRegister = async () => {
    clearTimeout(state.pollTimer);
    try { await api("DELETE", "/connection"); } catch {}
    $("#register-result").hidden = true;
    $("#register-go").hidden = false;
    $("#register-status").classList.remove("ok");
    $("#register-status").textContent = "Waiting for Connect…";
  };

  /* ---- step 4: camera ------------------------------------------------- */
  const renderCameras = (detected, selectedUrl) => {
    const container = $("#camera-list");
    container.innerHTML = "";
    if (!detected.length) {
      container.innerHTML = `<p class="help" style="margin:8px 0;">
        No Crowsnest streams detected. Enter a custom snapshot URL below
        or skip and configure later.</p>`;
      return;
    }
    for (const c of detected) {
      const row = document.createElement("div");
      row.className = "cam-row";
      if (c.snapshot_url === selectedUrl) row.classList.add("selected");
      row.innerHTML = `
        <div>
          <div class="cam-name">${c.name} <span style="color:var(--muted);font-size:12px;">(${c.mode}:${c.port})</span></div>
          <div class="cam-url">${c.snapshot_url}</div>
        </div>`;
      row.addEventListener("click", () => {
        document.querySelectorAll(".cam-row").forEach((r) =>
          r.classList.remove("selected"));
        row.classList.add("selected");
        state.selectedCamera = c;
        $("#custom-url").value = "";
      });
      container.appendChild(row);
    }
  };

  /* ---- step 5: dashboard --------------------------------------------- */
  const renderSummary = (conn, printer, cam) => {
    const dl = $("#summary");
    dl.innerHTML = "";
    const addRow = (k, v, cls) => {
      const dt = document.createElement("dt");
      dt.textContent = k;
      const dd = document.createElement("dd");
      dd.textContent = v ?? "—";
      if (cls) dd.classList.add(cls);
      dl.appendChild(dt);
      dl.appendChild(dd);
    };
    addRow("Printer type", printer.printer_type);
    addRow("Serial number", printer.serial_number);
    addRow("Firmware", printer.firmware);
    addRow("Fingerprint",
      printer.fingerprint ? printer.fingerprint.slice(0, 16) + "…" : null);
    addRow("Connect status",
      conn.status?.message ?? "—",
      conn.status?.ok ? "ok" : "bad");
    addRow("Token", conn.token ? conn.token.slice(0, 10) + "…" : "(none)",
      conn.token ? "ok" : "bad");
    addRow("Webcam",
      cam.selected?.snapshot_url || "(none)",
      cam.selected?.registered ? "ok" : null);
  };

  /* ---- navigation ----------------------------------------------------- */
  const gotoTypePicker = () => show("step-type");
  const gotoIdentity  = () => { fillIdentity(); show("step-identity"); };
  const gotoRegister  = () => {
    $("#register-result").hidden = true;
    $("#register-go").hidden = false;
    show("step-register");
  };
  const gotoCamera    = async () => {
    show("step-camera");
    try {
      const cams = await api("GET", "/cameras");
      renderCameras(cams.detected, cams.selected?.snapshot_url);
      if (cams.selected?.snapshot_url) {
        state.selectedCamera = {
          snapshot_url: cams.selected.snapshot_url,
          name: cams.selected.stream_name,
        };
      }
    } catch (e) {
      flashError(e.message);
    }
  };
  const gotoDashboard = async () => {
    show("step-done");
    try {
      const [p, c, cams] = await Promise.all([
        api("GET", "/printer"),
        api("GET", "/connection"),
        api("GET", "/cameras"),
      ]);
      renderSummary(c, p, cams);
    } catch (e) {
      flashError(e.message);
    }
  };

  /* ---- button wiring ------------------------------------------------- */
  document.addEventListener("DOMContentLoaded", async () => {
    try {
      const [p, c, pt] = await Promise.all([
        api("GET", "/printer"),
        api("GET", "/connection"),
        api("GET", "/printer-types"),
      ]);
      state.printer = p;
      state.connection = c;
      state.types = pt.types;
      renderTypes(pt.types, pt.current);

      // If already registered, jump straight to dashboard.
      if (c.registration === "FINISHED" && c.token) {
        gotoDashboard();
      } else {
        gotoTypePicker();
      }
    } catch (e) {
      flashError("Couldn't reach konnect service: " + e.message);
    }
  });

  $("#type-next").addEventListener("click", async () => {
    // printer_type is in konnect.cfg — we advise a restart if changed.
    if (state.selectedType && state.selectedType !== state.printer.printer_type) {
      flashError(
        "Selected type differs from konnect.cfg. Update " +
        "~/printer_data/config/konnect.cfg: printer_type = " +
        state.selectedType + " then restart konnect.service. " +
        "Continuing with the current value.",
      );
    }
    gotoIdentity();
  });
  $("#identity-back").addEventListener("click", gotoTypePicker);
  $("#identity-next").addEventListener("click", async () => {
    try {
      await api("POST", "/printer", {
        serial_number: $("#sn").value.trim(),
        name: $("#name").value.trim(),
        location: $("#location").value.trim(),
      });
      // Refresh in-memory copy to show updated SN in summary later.
      state.printer = await api("GET", "/printer");
      gotoRegister();
    } catch (e) {
      flashError(e.message);
    }
  });
  $("#register-back").addEventListener("click", gotoIdentity);
  $("#register-go").addEventListener("click", startRegister);
  $("#register-cancel").addEventListener("click", cancelRegister);

  $("#camera-skip").addEventListener("click", gotoDashboard);
  $("#camera-next").addEventListener("click", async () => {
    const custom = $("#custom-url").value.trim();
    const snap = custom || state.selectedCamera?.snapshot_url;
    if (!snap) {
      flashError("Pick a stream or enter a URL");
      return;
    }
    try {
      await api("POST", "/camera", {
        snapshot_url: snap,
        stream_name: state.selectedCamera?.name || "custom",
      });
      gotoDashboard();
    } catch (e) {
      flashError(e.message);
    }
  });

  $("#refresh").addEventListener("click", gotoDashboard);
  $("#unregister").addEventListener("click", async () => {
    if (!confirm("Unregister this printer from Prusa Connect?")) return;
    try {
      await api("DELETE", "/connection");
      state.printer = await api("GET", "/printer");
      state.connection = await api("GET", "/connection");
      gotoTypePicker();
    } catch (e) {
      flashError(e.message);
    }
  });
})();
