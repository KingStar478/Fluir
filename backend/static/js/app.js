(() => {
    const status = document.querySelector("[data-offline-status]");
    let lastConnectionState = null;
    let probeTimer = null;
    let databasePromise;

    const setStatus = (message = "") => {
        if (!status) return;
        status.textContent = message;
        status.hidden = !message;
    };
    window.fluirShowOfflineStatus = setStatus;

    const openDatabase = () => {
        if (!("indexedDB" in window)) return Promise.reject(new Error("Armazenamento local indisponível"));
        if (databasePromise) return databasePromise;
        databasePromise = new Promise((resolve, reject) => {
            const request = indexedDB.open("fluir-offline", 1);
            request.onupgradeneeded = () => {
                if (!request.result.objectStoreNames.contains("clima")) {
                    request.result.createObjectStore("clima", { keyPath: "key" });
                }
            };
            request.onsuccess = () => resolve(request.result);
            request.onerror = () => reject(request.error || new Error("Não foi possível abrir os dados salvos"));
        });
        return databasePromise;
    };
    const storeClimate = async (key, data) => {
        const db = await openDatabase();
        return new Promise((resolve, reject) => {
            const transaction = db.transaction("clima", "readwrite");
            transaction.objectStore("clima").put({ key, data, savedAt: Date.now() });
            transaction.oncomplete = () => resolve();
            transaction.onerror = () => reject(transaction.error || new Error("Falha ao salvar a previsão"));
        });
    };
    const readClimate = async (key) => {
        const db = await openDatabase();
        return new Promise((resolve, reject) => {
            const request = db.transaction("clima", "readonly").objectStore("clima").get(key);
            request.onsuccess = () => resolve(request.result || null);
            request.onerror = () => reject(request.error || new Error("Falha ao ler a previsão salva"));
        });
    };
    window.FluirOfflineStore = { saveClimate: storeClimate, loadClimate: readClimate };

    const updateConnection = (online, message = "") => {
        const changed = lastConnectionState !== null && online !== lastConnectionState;
        lastConnectionState = online;
        window.fluirInternetAvailable = online;
        if (message) setStatus(message);
        else if (online) setStatus("");
        else setStatus("Você está offline. O Fluir usará os dados salvos disponíveis.");
        if (changed) {
            document.dispatchEvent(new CustomEvent(online ? "fluir:online" : "fluir:offline"));
        }
    };

    const probeInternet = async () => {
        window.clearTimeout(probeTimer);
        if (!navigator.onLine) {
            updateConnection(false);
            scheduleProbe();
            return;
        }
        const controller = new AbortController();
        const timeout = window.setTimeout(() => controller.abort(), 4500);
        try {
            const url = "https://api.open-meteo.com/v1/forecast?latitude=-3.1&longitude=-60.0&current=temperature_2m&timezone=UTC";
            const response = await fetch(url, { cache: "no-store", signal: controller.signal });
            if (!response.ok) throw new Error("Internet indisponível");
            await response.body?.cancel();
            updateConnection(true);
        } catch (error) {
            updateConnection(false, "Sem internet. O Fluir usará os dados salvos disponíveis.");
        } finally {
            window.clearTimeout(timeout);
            scheduleProbe();
        }
    };
    const scheduleProbe = () => {
        const delay = document.visibilityState === "visible" ? 60000 : 180000;
        probeTimer = window.setTimeout(probeInternet, delay);
    };

    window.addEventListener("offline", () => updateConnection(false));
    window.addEventListener("online", probeInternet);
    document.addEventListener("visibilitychange", () => {
        if (document.visibilityState === "visible") probeInternet();
    });
    probeInternet();

    if ("serviceWorker" in navigator) {
        window.addEventListener("load", () => {
            navigator.serviceWorker.register("/service-worker.js", { scope: "/" }).catch(() => {
                // O Fluir continua funcionando; só não terá cache de arquivos para a próxima abertura.
            });
        }, { once: true });
    }
})();

const chatNavUnreadBadge = document.querySelector("[data-chat-nav-unread]");
if (chatNavUnreadBadge && !document.querySelector("[data-chat-page]")) {
    const updateChatNavUnread = async () => {
        try {
            const response = await fetch("/api/chat/unread-total", { cache: "no-store" });
            if (!response.ok) return;
            const payload = await response.json();
            const count = Math.max(0, Number(payload.count) || 0);
            chatNavUnreadBadge.textContent = count > 99 ? "99+" : String(count);
            chatNavUnreadBadge.hidden = count === 0;
            chatNavUnreadBadge.setAttribute("aria-label", `${count} mensagens não lidas`);
            chatNavUnreadBadge.closest("a")?.setAttribute(
                "aria-label",
                count ? `Chat, ${count} mensagens não lidas` : "Chat",
            );
        } catch (error) {
            // Mantém o último contador conhecido se o serviço de chat oscilar.
        }
    };
    let unreadRefreshTimer;
    const scheduleUnreadRefresh = () => {
        window.clearTimeout(unreadRefreshTimer);
        unreadRefreshTimer = window.setTimeout(async () => {
            await updateChatNavUnread();
            scheduleUnreadRefresh();
        }, document.visibilityState === "visible" ? 6000 : 20000);
    };
    const refreshUnreadWhenVisible = () => {
        if (document.visibilityState !== "visible") return;
        window.clearTimeout(unreadRefreshTimer);
        updateChatNavUnread().finally(scheduleUnreadRefresh);
    };
    refreshUnreadWhenVisible();
    window.addEventListener("focus", refreshUnreadWhenVisible);
    window.addEventListener("online", refreshUnreadWhenVisible);
    document.addEventListener("visibilitychange", refreshUnreadWhenVisible);
}

const appShell = document.querySelector(".app-shell");
if (appShell) {
    const menuToggle = appShell.querySelector("[data-mobile-menu-toggle]");
    const menuPanel = appShell.querySelector("[data-mobile-nav]");
    const menuBackdrop = appShell.querySelector("[data-mobile-menu-backdrop]");
    const menuClose = appShell.querySelector("[data-mobile-menu-close]");
    const compactMenu = window.matchMedia("(max-width: 800px)");
    const setMenuOpen = (open, returnFocus = false) => {
        const isOpen = open && compactMenu.matches;
        appShell.classList.toggle("mobile-nav-open", isOpen);
        menuToggle.setAttribute("aria-expanded", String(isOpen));
        menuToggle.setAttribute("aria-label", isOpen ? "Fechar menu de navegação" : "Abrir menu de navegação");
        menuPanel.setAttribute("aria-hidden", String(compactMenu.matches && !isOpen));
        menuBackdrop.hidden = !isOpen;
        if (returnFocus && compactMenu.matches) menuToggle.focus();
    };
    menuToggle.addEventListener("click", () => setMenuOpen(!appShell.classList.contains("mobile-nav-open")));
    menuClose.addEventListener("click", () => setMenuOpen(false, true));
    menuBackdrop.addEventListener("click", () => setMenuOpen(false, true));
    menuPanel.querySelectorAll("a").forEach((link) => link.addEventListener("click", () => setMenuOpen(false)));
    document.addEventListener("keydown", (event) => {
        if (event.key === "Escape" && appShell.classList.contains("mobile-nav-open")) setMenuOpen(false, true);
    });
    compactMenu.addEventListener("change", () => setMenuOpen(false));
    setMenuOpen(false);
}

document.querySelectorAll("[data-theme-toggle]").forEach((button) => {
    const update = () => {
        const dark = document.documentElement.dataset.theme === "dark";
        const label = dark ? "Tema escuro" : "Tema claro";
        button.setAttribute("aria-label", dark ? "Ativar tema claro" : "Ativar tema escuro");
        button.setAttribute("aria-pressed", String(dark));
        const labelTarget = button.querySelector("[data-theme-label]");
        if (labelTarget) labelTarget.textContent = label;
    };
    update();
    button.addEventListener("click", () => {
        const next = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
        document.documentElement.dataset.theme = next;
        localStorage.setItem("fluir-theme", next);
        document.cookie = `fluir-theme=${next}; Path=/; Max-Age=31536000; SameSite=Lax`;
        update();
    });
});

document.querySelectorAll("input[type='password']").forEach((field) => {
    if (field.closest(".password-input-wrap")) return;
    const wrapper = document.createElement("span");
    wrapper.className = "password-input-wrap";
    field.parentNode.insertBefore(wrapper, field);
    wrapper.appendChild(field);
    const toggle = document.createElement("button");
    toggle.type = "button";
    toggle.className = "password-visibility-toggle";
    toggle.setAttribute("aria-label", "Mostrar senha");
    const eyeIcon = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M2 12s3.6-7 10-7 10 7 10 7-3.6 7-10 7S2 12 2 12Z"/><circle cx="12" cy="12" r="3"/></svg>';
    const eyeOffIcon = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="m3 3 18 18M10.6 10.6a2 2 0 0 0 2.8 2.8M9.9 5.2A10.8 10.8 0 0 1 12 5c6.4 0 10 7 10 7a15.7 15.7 0 0 1-3.2 3.9M6.2 6.2C3.5 8 2 12 2 12s3.6 7 10 7a10.8 10.8 0 0 0 3.1-.5"/></svg>';
    toggle.innerHTML = eyeIcon;
    toggle.addEventListener("click", () => {
        const visible = field.type === "password";
        field.type = visible ? "text" : "password";
        toggle.innerHTML = visible ? eyeOffIcon : eyeIcon;
        toggle.setAttribute("aria-label", visible ? "Ocultar senha" : "Mostrar senha");
        toggle.setAttribute("aria-pressed", String(visible));
    });
    wrapper.appendChild(toggle);
});

document.querySelectorAll("[data-email-name]").forEach((nameField) => {
    const preview = nameField.form?.querySelector("[data-email-preview]");
    if (!preview) return;
    const update = () => {
        const names = nameField.value.normalize("NFD").replace(/[\u0300-\u036f]/g, "").match(/[a-zA-Z0-9]+/g) || [];
        const relevant = names.filter((part) => !["de", "da", "do", "das", "dos", "e"].includes(part.toLowerCase()));
        const first = relevant[0] || "F";
        const last = relevant.length > 1 ? relevant[relevant.length - 1] : first;
        preview.value = `${first[0]}${last[0]}`.toUpperCase() + "@fluir.com";
    };
    nameField.addEventListener("input", update);
    update();
});

document.querySelectorAll("input[type='file'][data-max-files]").forEach((field) => {
    field.addEventListener("change", () => {
        const maximum = Number(field.dataset.maxFiles) || 3;
        if (field.files.length <= maximum) return;
        field.value = "";
        const toast = document.createElement("div");
        toast.className = "app-toast app-toast-warning";
        toast.setAttribute("role", "status");
        toast.textContent = `Selecione no máximo ${maximum} imagens.`;
        document.body.appendChild(toast);
        window.setTimeout(() => { toast.classList.add("app-toast-leaving"); window.setTimeout(() => toast.remove(), 300); }, 2600);
    });
});

document.addEventListener("submit", (event) => {
    const form = event.target.closest("form[data-confirm-message]");
    if (!form || form.dataset.confirmed === "true") return;
    event.preventDefault();
    let dialog = document.querySelector("[data-generic-confirm-dialog]");
    if (!dialog) {
        dialog = document.createElement("dialog");
        dialog.className = "chat-confirm-dialog";
        dialog.dataset.genericConfirmDialog = "true";
        dialog.innerHTML = '<form method="dialog" class="chat-confirm-card"><h2 data-generic-confirm-title>Confirmar ação</h2><p data-generic-confirm-message></p><div><button class="secondary-button" value="cancel">Cancelar</button><button class="primary-button" value="confirm" data-generic-confirm-action>Confirmar</button></div></form>';
        document.body.appendChild(dialog);
    }
    dialog.querySelector("[data-generic-confirm-title]").textContent = form.dataset.confirmTitle || "Confirmar ação";
    dialog.querySelector("[data-generic-confirm-message]").textContent = form.dataset.confirmMessage;
    dialog.querySelector("[data-generic-confirm-action]").textContent = form.dataset.confirmAction || "Confirmar";
    const modalForm = dialog.querySelector("form");
    modalForm.onsubmit = (choiceEvent) => {
        if (choiceEvent.submitter?.value !== "confirm") return;
        choiceEvent.preventDefault();
        form.dataset.confirmed = "true";
        dialog.close();
        form.requestSubmit();
    };
    dialog.showModal();
}, true);

document.addEventListener("submit", (event) => {
    const form = event.target;
    // Dialog forms only close their dialog; they are not network submissions.
    // Marking their buttons as "Processando..." leaves the confirmation UI
    // looking stuck (and some browsers do not expose the clicked submitter).
    if (!(form instanceof HTMLFormElement) || form.method === "dialog" || form.matches("[data-chat-form]")) return;
    if (form.hasAttribute("data-confirm-message") && form.dataset.confirmed !== "true") return;
    const submitter = event.submitter || form.querySelector("button[type='submit'], input[type='submit']");
    if (!submitter || submitter.disabled) return;
    // Keep the clicked submit button's name/value in the POST body. A disabled
    // submit button is omitted from form submission, so disabling it here was
    // dropping `decisao=saiu|adiar` and making the server reject the request.
    if (submitter.name) {
        let mirror = form.querySelector("[data-submit-button-value]");
        if (mirror) mirror.remove();
        mirror = document.createElement("input");
        mirror.type = "hidden";
        mirror.name = submitter.name;
        mirror.value = submitter.value;
        mirror.dataset.submitButtonValue = "true";
        form.appendChild(mirror);
    }
    submitter.disabled = true;
    if (submitter instanceof HTMLButtonElement) submitter.textContent = "Processando...";
}, false);

document.querySelectorAll(".app-toast").forEach((toast) => {
    window.setTimeout(() => {
        toast.classList.add("app-toast-leaving");
        window.setTimeout(() => toast.remove(), 300);
    }, 2600);
});

const profileFile = document.querySelector("[data-profile-file]");
if (profileFile) {
    const form = profileFile.closest("[data-profile-photo-form]");
    const avatar = profileFile.closest(".profile-avatar");
    const preview = avatar.querySelector("[data-profile-preview]");
    const save = form.querySelector("[data-save-profile-photo]");
    const setFile = (file) => {
        if (!file || !file.type.startsWith("image/")) return;
        const transfer = new DataTransfer();
        transfer.items.add(file);
        profileFile.files = transfer.files;
        preview.src = URL.createObjectURL(file);
        preview.hidden = false;
        save.hidden = false;
        avatar.classList.add("profile-avatar-has-preview");
    };
    profileFile.addEventListener("change", () => setFile(profileFile.files?.[0]));
    avatar.addEventListener("dragover", (event) => { event.preventDefault(); avatar.classList.add("profile-avatar-dragover"); });
    avatar.addEventListener("dragleave", () => avatar.classList.remove("profile-avatar-dragover"));
    avatar.addEventListener("drop", (event) => { event.preventDefault(); avatar.classList.remove("profile-avatar-dragover"); setFile(event.dataTransfer?.files?.[0]); });
}

document.querySelector("[data-profile-edit-toggle]")?.addEventListener("click", () => {
    const gate = document.querySelector("[data-profile-password-gate]");
    gate.hidden = !gate.hidden;
    if (!gate.hidden) gate.querySelector("input")?.focus();
});

document.querySelector("[data-password-change-toggle]")?.addEventListener("click", (event) => {
    const button = event.currentTarget;
    const form = document.querySelector("[data-password-change-form]");
    form.hidden = !form.hidden;
    button.setAttribute("aria-expanded", String(!form.hidden));
    if (!form.hidden) form.querySelector("input")?.focus();
});

document.querySelectorAll("[data-port-code]").forEach((field) => {
    field.addEventListener("input", () => {
        field.value = field.value.replace(/[^a-z0-9]/gi, "").toUpperCase().slice(0, 5);
    });
});

document.querySelectorAll("[data-tabs]").forEach((tabs) => {
    const buttons = tabs.querySelectorAll("[data-tab-button]");
    const panels = tabs.querySelectorAll("[data-tab-panel]");

    buttons.forEach((button) => {
        button.addEventListener("click", () => {
            const target = button.dataset.tabButton;
            buttons.forEach((item) => item.classList.toggle("active", item === button));
            panels.forEach((panel) => {
                panel.classList.toggle("active", panel.dataset.tabPanel === target);
            });
        });
    });
});

const portMap = document.querySelector("#port-map");
if (portMap && window.L) {
    const points = JSON.parse(portMap.dataset.points || "[]");
    const map = L.map(portMap, { scrollWheelZoom: false });

    L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
        attribution: "&copy; OpenStreetMap contributors",
    }).addTo(map);

    const markers = points.map((point) => {
        const marker = L.marker([point.lat, point.lon]).addTo(map);
        marker.bindPopup(`
            <strong>${point.nome}</strong><br>
            ${point.cidade}<br>
            Hidrovia: ${point.hidrovia}<br>
            Latitude: ${point.lat}<br>
            Longitude: ${point.lon}<br>
            Fonte: ${point.fonte}
        `);
        return marker;
    });

    if (markers.length) {
        const group = L.featureGroup(markers);
        map.fitBounds(group.getBounds().pad(0.2));
    } else {
        map.setView([-2.5, -54.0], 5);
    }
}

const droughtWidget = document.querySelector("[data-drought-widget]");
if (droughtWidget) {
    const level = droughtWidget.querySelector("[data-drought-level]");
    const detail = droughtWidget.querySelector("[data-drought-detail]");
    const refreshButtons = document.querySelectorAll("[data-drought-refresh]");
    const latitude = droughtWidget.dataset.lat;
    const longitude = droughtWidget.dataset.lon;
    const loadingTargets = [droughtWidget, document.querySelector("[data-rain-card]"), document.querySelector("[data-wind-card]"), document.querySelector("[data-weather-chart-container]")].filter(Boolean);

    const renderRainForecast = (forecast = [], current = {}, total = null) => {
        const svg = document.querySelector("[data-rain-chart]");
        if (!svg) return;
        const ns = "http://www.w3.org/2000/svg";
        const width = 840;
        const height = 290;
        const margin = { top: 28, right: 58, bottom: 42, left: 56 };
        const plotWidth = width - margin.left - margin.right;
        const plotHeight = height - margin.top - margin.bottom;
        const rows = forecast.slice(0, 24);
        svg.replaceChildren();

        const add = (tag, attrs = {}, text = "") => {
            const node = document.createElementNS(ns, tag);
            Object.entries(attrs).forEach(([key, value]) => node.setAttribute(key, String(value)));
            if (text) node.textContent = text;
            svg.appendChild(node);
            return node;
        };

        if (!rows.length) {
            add("text", { x: width / 2, y: height / 2, "text-anchor": "middle", class: "weather-chart-empty" }, "A previsão horária não está disponível agora. Tente atualizar em instantes.");
            return;
        }

        const values = rows.map((row) => Number(row.chuva_mm) || 0);
        const maxRain = Math.max(1, Math.ceil(Math.max(...values) * 1.2));
        const step = maxRain <= 4 ? 1 : Math.ceil(maxRain / 4);
        const yMax = Math.max(step * 4, maxRain);
        const yRain = (value) => margin.top + plotHeight - (value / yMax) * plotHeight;
        const yProb = (value) => margin.top + plotHeight - (Math.max(0, Math.min(100, Number(value) || 0)) / 100) * plotHeight;
        const xCenter = (index) => margin.left + ((index + 0.5) / rows.length) * plotWidth;

        const defs = document.createElementNS(ns, "defs");
        defs.innerHTML = '<linearGradient id="rain-bar-gradient" x1="0" x2="0" y1="0" y2="1"><stop offset="0%" stop-color="#38bdf8"/><stop offset="100%" stop-color="#2563eb"/></linearGradient><linearGradient id="rain-area-gradient" x1="0" x2="0" y1="0" y2="1"><stop offset="0%" stop-color="#f59e0b" stop-opacity=".20"/><stop offset="100%" stop-color="#f59e0b" stop-opacity="0"/></linearGradient>';
        svg.appendChild(defs);

        for (let tick = 0; tick <= yMax; tick += step) {
            const y = yRain(tick);
            add("line", { x1: margin.left, y1: y, x2: width - margin.right, y2: y, class: "weather-chart-gridline" });
            add("text", { x: margin.left - 12, y: y + 4, "text-anchor": "end", class: "weather-chart-axis" }, String(tick));
        }
        [0, 50, 100].forEach((tick) => {
            const y = yProb(tick);
            add("text", { x: width - margin.right + 12, y: y + 4, "text-anchor": "start", class: "weather-chart-axis" }, `${tick}%`);
        });
        add("text", { x: 16, y: margin.top + plotHeight / 2, transform: `rotate(-90 16 ${margin.top + plotHeight / 2})`, class: "weather-chart-axis-title" }, "mm/h");

        const slot = plotWidth / rows.length;
        const barWidth = Math.max(5, Math.min(22, slot * 0.5));
        rows.forEach((row, index) => {
            const value = values[index];
            const x = xCenter(index) - barWidth / 2;
            const y = yRain(value);
            const rect = add("rect", { x, y, width: barWidth, height: Math.max(2, margin.top + plotHeight - y), rx: 4, class: value > 0 ? "weather-rain-bar" : "weather-rain-bar weather-rain-bar-empty" });
            const title = document.createElementNS(ns, "title");
            const timeLabel = index === 0 ? "Agora" : (row.hora || "").slice(11, 16);
            title.textContent = `${timeLabel}: ${value.toFixed(1)} mm/h · ${Number(row.probabilidade) || 0}% de chance de chuva`;
            rect.appendChild(title);
            if (index % 4 === 0 || index === rows.length - 1) {
                add("text", { x: xCenter(index), y: height - 13, "text-anchor": "middle", class: "weather-chart-axis" }, index === 0 ? "Agora" : timeLabel);
            }
        });

        const probabilityPoints = rows.map((row, index) => `${xCenter(index)},${yProb(row.probabilidade)}`).join(" ");
        const probabilityArea = `${xCenter(0)},${margin.top + plotHeight} ${probabilityPoints} ${xCenter(rows.length - 1)},${margin.top + plotHeight}`;
        add("polygon", { points: probabilityArea, class: "weather-probability-area" });
        add("polyline", { points: probabilityPoints, class: "weather-probability-line" });
        rows.forEach((row, index) => {
            add("circle", { cx: xCenter(index), cy: yProb(row.probabilidade), r: 3, class: "weather-probability-point" });
        });

        const currentTemp = Number(current.temperatura_c);
        const currentHumidity = Number(current.umidade_pct);
        const currentRain = Number(current.chuva_mm);
        const currentWind = Number(current.vento_kmh);
        const currentGust = Number(current.rajada_kmh);
        document.querySelector("[data-current-weather]")?.replaceChildren(document.createTextNode(
            `${Number.isFinite(currentTemp) ? `${Math.round(currentTemp)}°C` : "--°C"} · ${Number.isFinite(currentHumidity) ? `${Math.round(currentHumidity)}% umidade` : "--% umidade"}`
        ));
        document.querySelector("[data-current-rain]")?.replaceChildren(document.createTextNode(`${Number.isFinite(currentRain) ? currentRain.toFixed(1) : "--"} mm/h`));
        document.querySelector("[data-current-wind]")?.replaceChildren(document.createTextNode(
            `${Number.isFinite(currentWind) ? Math.round(currentWind) : "--"} / ${Number.isFinite(currentGust) ? Math.round(currentGust) : "--"} km/h`
        ));
        document.querySelector("[data-rain-total]")?.replaceChildren(document.createTextNode(`${Number.isFinite(Number(total)) ? Number(total).toFixed(1) : "--"} mm`));
        document.querySelector("[data-wind-level]")?.replaceChildren(document.createTextNode(
            Number.isFinite(currentGust) ? `${Math.round(currentGust)} km/h` : "Indisponível"
        ));
        document.querySelector("[data-wind-detail]")?.replaceChildren(document.createTextNode(
            Number.isFinite(currentWind) ? `Vento médio estimado: ${Math.round(currentWind)} km/h. Rajadas: ${Math.round(currentGust)} km/h.` : "Não foi possível consultar o vento atual."
        ));
    };

    const calculateDroughtRisk = (rainTotal) => {
        if (rainTotal <= 5) {
            return {
                nivel: "Alto",
                classe: "alta",
                mensagem: "Pouca chuva prevista. Risco maior de restrição operacional por seca.",
            };
        }
        if (rainTotal <= 15) {
            return {
                nivel: "Moderado",
                classe: "moderada",
                mensagem: "Chuva prevista abaixo do ideal. Acompanhe calado e janelas de atracação.",
            };
        }
        return {
            nivel: "Baixo",
            classe: "baixa",
            mensagem: "Previsão de chuva reduz o risco imediato de seca operacional.",
        };
    };

    const setClass = (statusClass) => {
        droughtWidget.classList.remove(
            "drought-loading",
            "drought-alta",
            "drought-moderada",
            "drought-baixa",
            "drought-indisponivel",
            "alert-normal",
            "alert-attention",
            "alert-risk",
            "alert-critical",
        );
        droughtWidget.classList.add(`drought-${statusClass}`);
        const alertClass = {
            baixa: "alert-normal",
            moderada: "alert-attention",
            alta: "alert-critical",
            indisponivel: "alert-attention",
            loading: "alert-attention",
        }[statusClass] || "alert-attention";
        droughtWidget.classList.add(alertClass);
    };

    const renderDroughtData = (data) => {
        setClass(data.classe || "indisponivel");
        level.textContent = data.nivel;
        const rainText = data.chuva_7d_mm === null ? "" : ` Chuva prevista em 7 dias: ${data.chuva_7d_mm} mm.`;
        detail.textContent = `${data.mensagem}${rainText}`;
        renderRainForecast(data.previsao_horaria, data.clima_atual, data.chuva_24h_mm);
        renderClimateDashboard(data);
    };

    const classMap = {
        normal: "alert-normal",
        baixa: "alert-normal",
        atencao: "alert-attention",
        moderada: "alert-attention",
        risco: "alert-risk",
        critico: "alert-critical",
        alta: "alert-critical",
        indisponivel: "alert-attention",
    };

    const normalizeStatus = (value) => (value || "").normalize("NFD").replace(/[\u0300-\u036f]/g, "").toLowerCase();

    const setAlertClass = (element, statusClass) => {
        if (!element) {
            return;
        }
        element.classList.remove("alert-normal", "alert-attention", "alert-risk", "alert-critical");
        element.classList.add(classMap[statusClass] || "alert-attention");
    };

    const renderClimateDashboard = (data) => {
        const rainCard = document.querySelector("[data-rain-card]");
        const windCard = document.querySelector("[data-wind-card]");
        const activeAlertCard = document.querySelector("[data-active-alert-card]");

        const alertCount = data.alertas_ativos ?? 0;
        const displayedAlertCount = alertCount + 2; // inclui os dois alertas de exemplo fixos do painel
        const alertCountBadge = document.querySelector("[data-active-alert-count]");
        if (alertCountBadge) {
            alertCountBadge.textContent = String(displayedAlertCount);
            alertCountBadge.hidden = false;
        }
        activeAlertCard?.setAttribute("aria-label", `Abrir alertas: ${displayedAlertCount} itens, incluindo 2 exemplos demonstrativos${alertCount ? ` e ${alertCount} alerta(s) calculado(s) pela previsão` : ""}`);
        document.querySelector("[data-rain-level]")?.replaceChildren(document.createTextNode(data.chuva_nivel || "Indisponível"));
        document.querySelector("[data-rain-detail]")?.replaceChildren(document.createTextNode(`${data.chuva_mensagem || "Não foi possível consultar a chuva."} Chuva em 24 horas: ${data.chuva_24h_mm ?? "?"} mm.`));
        const alertSummary = document.querySelector("[data-active-alert-summary]");
        if (alertSummary) {
            alertSummary.replaceChildren();
            const alerts = [];
            if (["moderada", "alta"].includes(normalizeStatus(data.classe))) {
                alerts.push({ title: `Risco de seca: ${data.nivel || "atenção"}`, detail: data.mensagem || "A previsão indica redução de chuva e possível impacto na operação." });
            }
            if (["risco", "critico"].includes(normalizeStatus(data.chuva_classe))) {
                alerts.push({ title: `Chuva intensa: ${data.chuva_nivel || "atenção"}`, detail: `${data.chuva_mensagem || "Há previsão de chuva forte."} Chuva prevista em 24 horas: ${data.chuva_24h_mm ?? "?"} mm.` });
            }
            alerts.forEach((alert) => {
                const item = document.createElement("article");
                const title = document.createElement("strong");
                const detail = document.createElement("p");
                title.textContent = alert.title;
                detail.textContent = alert.detail;
                item.append(title, detail);
                alertSummary.appendChild(item);
            });
            if (!alerts.length) {
                const emptyMessage = document.createElement("p");
                emptyMessage.className = "active-alert-empty";
                emptyMessage.textContent = "Nenhum alerta real foi calculado na última previsão consultada.";
                alertSummary.appendChild(emptyMessage);
            }
        }
        setAlertClass(rainCard, data.chuva_classe);
        setAlertClass(activeAlertCard, normalizeStatus(data.alerta_geral));
        setAlertClass(windCard, "normal");
    };

    const activeAlertDialog = document.querySelector("[data-active-alert-dialog]");
    document.querySelector("[data-active-alert-card]")?.addEventListener("click", () => activeAlertDialog?.showModal());
    document.querySelector("[data-active-alert-close]")?.addEventListener("click", () => activeAlertDialog?.close());
    activeAlertDialog?.addEventListener("click", (event) => {
        if (event.target === activeAlertDialog) activeAlertDialog.close();
    });

    const fetchFromBrowser = async () => {
        const params = new URLSearchParams({
            latitude,
            longitude,
            current: "temperature_2m,relative_humidity_2m,precipitation,wind_speed_10m,wind_gusts_10m",
            hourly: "precipitation,precipitation_probability,wind_speed_10m,wind_gusts_10m",
            daily: "precipitation_sum",
            forecast_days: "7",
            timezone: "America/Manaus",
        });
        const controller = new AbortController();
        const timeout = window.setTimeout(() => controller.abort(), 6000);
        let response;
        try {
            response = await fetch(`https://api.open-meteo.com/v1/forecast?${params.toString()}`, {
                cache: "no-store",
                signal: controller.signal,
            });
        } finally {
            window.clearTimeout(timeout);
        }
        if (!response.ok) {
            throw new Error("Open-Meteo unavailable");
        }
        const payload = await response.json();
        const values = payload.daily?.precipitation_sum || [];
        const hourly = payload.hourly?.precipitation || [];
        const rainTotal = Math.round(values.reduce((sum, value) => sum + Number(value || 0), 0) * 10) / 10;
        const current = payload.current || {};
        const times = payload.hourly?.time || [];
        const currentTime = current.time || "";
        const start = Math.max(0, times.findIndex((time) => time >= currentTime));
        const hourlyRows = times.slice(start, start + 24).map((time, index) => ({
            hora: time,
            chuva_mm: payload.hourly?.precipitation?.[start + index] ?? 0,
            probabilidade: payload.hourly?.precipitation_probability?.[start + index] ?? 0,
            vento_kmh: payload.hourly?.wind_speed_10m?.[start + index] ?? 0,
            rajada_kmh: payload.hourly?.wind_gusts_10m?.[start + index] ?? 0,
        }));
        const rain24h = Math.round(hourlyRows.reduce((sum, row) => sum + Number(row.chuva_mm || 0), 0) * 10) / 10;
        const risk = calculateDroughtRisk(rainTotal);
        const rainRisk = rain24h >= 50
            ? { nivel: "Crítico", classe: "critico", mensagem: "Chuvas muito fortes previstas para as próximas 24 horas." }
            : rain24h >= 30
                ? { nivel: "Risco", classe: "risco", mensagem: "Chuvas fortes previstas para as próximas 24 horas." }
                : rain24h >= 10
                    ? { nivel: "Atenção", classe: "atencao", mensagem: "Chuva moderada prevista. Acompanhe o pátio e as cargas sensíveis." }
                    : { nivel: "Normal", classe: "normal", mensagem: "Sem indício de chuva intensa nas próximas 24 horas." };
        const activeAlerts = [
            ["moderada", "alta"].includes(risk.classe),
            ["risco", "critico"].includes(rainRisk.classe),
        ].filter(Boolean).length;
        return {
            porto: droughtWidget.dataset.port,
            codigo: droughtWidget.dataset.code,
            hidrovia: droughtWidget.dataset.waterway,
            chuva_7d_mm: rainTotal,
            chuva_24h_mm: rain24h,
            clima_atual: {
                hora: current.time,
                temperatura_c: current.temperature_2m,
                umidade_pct: current.relative_humidity_2m,
                chuva_mm: current.precipitation,
                vento_kmh: current.wind_speed_10m,
                rajada_kmh: current.wind_gusts_10m,
            },
            previsao_horaria: hourlyRows,
            nivel: risk.nivel,
            classe: risk.classe,
            mensagem: risk.mensagem,
            chuva_nivel: rainRisk.nivel,
            chuva_classe: rainRisk.classe,
            chuva_mensagem: rainRisk.mensagem,
            alertas_ativos: activeAlerts,
            alerta_geral: activeAlerts ? "Atencao" : "Normal",
            nivel_rio: {
                disponivel: false,
                valor: "Não disponível",
                tendencia: "Não disponível",
                variacao: "",
                mensagem: "Acompanhe as medições das estações da bacia do Amazonas no portal oficial SGB/SACE.",
                fonte: "SGB/SACE · Bacia do Amazonas",
            },
            fonte: "Open-Meteo Forecast API",
        };
    };

    const loadDroughtRisk = async () => {
        setClass("loading");
        level.textContent = "Calculando...";
        detail.textContent = "Buscando previsão e recalculando os indicadores.";
        document.querySelector("[data-rain-level]").textContent = "Buscando...";
        document.querySelector("[data-rain-detail]").textContent = "Consultando previsão de chuva para as próximas 24 horas.";
        document.querySelector("[data-wind-level]").textContent = "Consultando...";
        document.querySelector("[data-wind-detail]").textContent = "Atualizando condições estimadas do vento.";
        refreshButtons.forEach((button) => { button.disabled = true; button.textContent = "Atualizando..."; });
        loadingTargets.forEach((element) => { element.classList.add("climate-updating"); element.setAttribute("aria-busy", "true"); });

        const cacheKey = `clima:${droughtWidget.dataset.code || "porto"}`;
        const saveFreshSnapshot = async (data) => {
            try {
                await window.FluirOfflineStore?.saveClimate(cacheKey, data);
            } catch (error) {
                // A previsão atual continua visível mesmo se o navegador bloquear o armazenamento.
            }
        };
        const showSavedSnapshot = async () => {
            try {
                const snapshot = await window.FluirOfflineStore?.loadClimate(cacheKey);
                if (!snapshot?.data) return false;
                renderDroughtData(snapshot.data);
                const savedAt = new Date(snapshot.savedAt).toLocaleString("pt-BR", { dateStyle: "short", timeStyle: "short" });
                window.fluirShowOfflineStatus?.(`Você está offline. Dados do painel salvos em ${savedAt}.`);
                return true;
            } catch (error) {
                return false;
            }
        };

        try {
            if ((!navigator.onLine || window.fluirInternetAvailable === false) && await showSavedSnapshot()) return;
            const response = await fetch("/api/seca", { cache: "no-store" });
            if (!response.ok) {
                throw new Error("Backend drought API unavailable");
            }
            const data = await response.json();
            renderDroughtData(data);
            await saveFreshSnapshot(data);
            window.fluirInternetAvailable = true;
            if (navigator.onLine) window.fluirShowOfflineStatus?.("");
        } catch (error) {
            try {
                const data = await fetchFromBrowser();
                renderDroughtData(data);
                await saveFreshSnapshot(data);
                window.fluirInternetAvailable = true;
                window.fluirShowOfflineStatus?.("");
            } catch (browserError) {
                if (await showSavedSnapshot()) {
                    return;
                }
                window.fluirShowOfflineStatus?.("Sem internet. Não há dados salvos do painel neste aparelho ainda.");
                setClass("indisponivel");
                level.textContent = "Indisponível";
                detail.textContent = "Não foi possível consultar a previsão real de chuva agora. Verifique a conexão com a internet.";
                document.querySelector("[data-rain-level]").textContent = "Indisponível";
                document.querySelector("[data-rain-detail]").textContent = "Não foi possível atualizar os dados de chuva.";
                const alertCountBadge = document.querySelector("[data-active-alert-count]");
                if (alertCountBadge) {
                    alertCountBadge.textContent = "2";
                    alertCountBadge.hidden = false;
                }
                const alertSummary = document.querySelector("[data-active-alert-summary]");
                if (alertSummary) {
                    const emptyMessage = document.createElement("p");
                    emptyMessage.className = "active-alert-empty";
                    emptyMessage.textContent = "Não foi possível consultar alertas reais agora. Os dois avisos abaixo são apenas exemplos.";
                    alertSummary.replaceChildren(emptyMessage);
                }
                renderRainForecast([], {}, null);
                document.querySelector("[data-current-weather]").textContent = "Indisponível";
                document.querySelector("[data-current-rain]").textContent = "Indisponível";
                document.querySelector("[data-current-wind]").textContent = "Indisponível";
                document.querySelector("[data-rain-total]").textContent = "Indisponível";
                document.querySelector("[data-wind-level]").textContent = "Indisponível";
                document.querySelector("[data-wind-detail]").textContent = "Não foi possível atualizar o vento e a previsão horária.";
            }
        } finally {
            refreshButtons.forEach((button) => { button.disabled = false; button.textContent = "Atualizar"; });
            loadingTargets.forEach((element) => { element.classList.remove("climate-updating"); element.removeAttribute("aria-busy"); });
        }
    };

    refreshButtons.forEach((button) => {
        button.addEventListener("click", loadDroughtRisk);
    });
    document.addEventListener("fluir:offline", loadDroughtRisk);
    document.addEventListener("fluir:online", loadDroughtRisk);
    loadDroughtRisk();
}

const legendHelps = document.querySelectorAll(".legend-help");
if (legendHelps.length) {
    legendHelps.forEach((legendHelp) => {
        const helpButton = legendHelp.querySelector(".help-button");

        helpButton?.addEventListener("click", (event) => {
            event.stopPropagation();
            legendHelps.forEach((item) => {
                if (item !== legendHelp) {
                    item.classList.remove("legend-help-open");
                }
            });
            legendHelp.classList.toggle("legend-help-open");
        });
    });

    document.addEventListener("click", (event) => {
        legendHelps.forEach((legendHelp) => {
            if (!legendHelp.contains(event.target)) {
                legendHelp.classList.remove("legend-help-open");
            }
        });
    });

    document.addEventListener("keydown", (event) => {
        if (event.key === "Escape") {
            legendHelps.forEach((legendHelp) => legendHelp.classList.remove("legend-help-open"));
        }
    });
}

const eventModal = document.querySelector("[data-event-modal]");
if (eventModal) {
    const fields = {
        title: eventModal.querySelector("[data-event-title]"),
        type: eventModal.querySelector("[data-event-type]"),
        status: eventModal.querySelector("[data-event-status]"),
        carga: eventModal.querySelector("[data-event-carga]"),
        navioIdeal: eventModal.querySelector("[data-event-navio-ideal]"),
        fclLcl: eventModal.querySelector("[data-event-fcl-lcl]"),
        tipoNavio: eventModal.querySelector("[data-event-tipo-navio]"),
        temperatura: eventModal.querySelector("[data-event-temperatura]"),
        capacidade: eventModal.querySelector("[data-event-capacidade]"),
        eta: eventModal.querySelector("[data-event-eta]"),
        etb: eventModal.querySelector("[data-event-etb]"),
        ets: eventModal.querySelector("[data-event-ets]"),
        embarcacao: eventModal.querySelector("[data-event-embarcacao]"),
        viagem: eventModal.querySelector("[data-event-viagem]"),
        armador: eventModal.querySelector("[data-event-armador]"),
        calado: eventModal.querySelector("[data-event-calado]"),
        dimensoes: eventModal.querySelector("[data-event-dimensoes]"),
        bandeira: eventModal.querySelector("[data-event-bandeira]"),
        peso: eventModal.querySelector("[data-event-peso]"),
        gateCutoff: eventModal.querySelector("[data-event-gate-cutoff]"),
        cargoDocCutoff: eventModal.querySelector("[data-event-cargo-doc-cutoff]"),
        planoCarga: eventModal.querySelector("[data-event-plano-carga]"),
        portosEscala: eventModal.querySelector("[data-event-portos-escala]"),
        observacao: eventModal.querySelector("[data-event-observacao]"),
    };

    const fillText = (field, value) => {
        field.textContent = value || "Não informado";
    };

    const closeModal = () => {
        eventModal.hidden = true;
        document.body.classList.remove("modal-open");
    };

    document.querySelectorAll("[data-event-details]").forEach((button) => {
        button.addEventListener("click", () => {
            const event = JSON.parse(button.dataset.eventDetails);
            const statusForm = eventModal.querySelector("[data-event-status-form]");
            const completeForm = eventModal.querySelector("[data-event-complete-form]");
            if (statusForm && event.id) {
                statusForm.action = `/agendamentos/${encodeURIComponent(event.id)}/status`;
                const statusSelect = statusForm.querySelector('[name="status"]');
                if (statusSelect) {
                    const allowedStatuses = [...statusSelect.options].map((option) => option.value);
                    statusSelect.value = allowedStatuses.includes(event.status) ? event.status : "Agendado";
                }
            }
            if (completeForm && event.id) {
                completeForm.action = `/agendamentos/${encodeURIComponent(event.id)}/concluir`;
                completeForm.hidden = !event.nome_embarcacao || ["concluído", "concluido"].includes((event.status || "").trim().toLocaleLowerCase("pt-BR"));
            }
            fillText(fields.title, event.nome_embarcacao || event.texto);
            fillText(fields.type, event.cor_label || event.tipo_carga);
            fillText(fields.status, event.status);
            fillText(fields.carga, event.tipo_carga);
            fillText(fields.navioIdeal, event.navio_ideal);
            fillText(fields.fclLcl, event.fcl_lcl);
            fillText(fields.tipoNavio, event.tipo_navio);
            fillText(fields.temperatura, event.temperatura);
            fillText(fields.capacidade, event.capacidade_navio);
            fillText(fields.eta, event.eta);
            fillText(fields.etb, event.etb);
            fillText(fields.ets, event.ets);
            fillText(fields.embarcacao, event.nome_embarcacao);
            fillText(fields.viagem, event.numero_viagem);
            fillText(fields.armador, event.armador);
            fillText(fields.calado, event.calado);
            fillText(fields.dimensoes, event.dimensoes);
            fillText(fields.bandeira, event.bandeira);
            fillText(fields.peso, event.peso_bruto);
            fillText(fields.gateCutoff, event.gate_cutoff);
            fillText(fields.cargoDocCutoff, event.cargo_doc_cutoff);
            fillText(fields.planoCarga, event.plano_carga);
            fillText(fields.portosEscala, event.portos_escala);
            fillText(fields.observacao, event.observacao);
            eventModal.hidden = false;
            document.body.classList.add("modal-open");
        });
    });

    eventModal.querySelectorAll("[data-event-close]").forEach((button) => {
        button.addEventListener("click", closeModal);
    });

    document.addEventListener("keydown", (event) => {
        if (event.key === "Escape" && !eventModal.hidden) {
            closeModal();
        }
    });
}

const scheduleModal = document.querySelector("[data-schedule-modal]");
if (scheduleModal) {
    const openButton = document.querySelector("[data-schedule-open]");
    const closeModal = () => {
        scheduleModal.hidden = true;
        document.body.classList.remove("modal-open");
    };

    openButton?.addEventListener("click", () => {
        scheduleModal.hidden = false;
        document.body.classList.add("modal-open");
    });

    scheduleModal.querySelectorAll("[data-schedule-close]").forEach((button) => {
        button.addEventListener("click", closeModal);
    });

    document.addEventListener("keydown", (event) => {
        if (event.key === "Escape" && !scheduleModal.hidden) {
            closeModal();
        }
    });
}
