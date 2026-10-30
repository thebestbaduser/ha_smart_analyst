class HaSmartAnalystPanel extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._tab = "overview";
    this._data = null;
    this._error = null;
  }

  set hass(hass) {
    const first = !this._hass;
    this._hass = hass;
    if (first) {
      this._load();
    }
  }

  get hass() {
    return this._hass;
  }

  connectedCallback() {
    this._render();
  }

  async _load() {
    if (!this.hass) {
      return;
    }
    try {
      this._data = await this.hass.callWS({ type: "ha_smart_analyst/overview" });
      this._error = null;
    } catch (err) {
      this._error = err && err.message ? err.message : String(err);
    }
    this._render();
  }

  _tabs() {
    return [
      ["overview", "Обзор"],
      ["systems", "Системы и датчики"],
      ["links", "Связи"],
      ["rules", "Правила"],
      ["incidents", "Инциденты"],
      ["reports", "Отчёты"],
      ["ai", "ИИ"],
      ["telegram", "Telegram"],
      ["history", "История"],
      ["diagnostics", "Диагностика"],
    ];
  }

  _render() {
    if (!this.shadowRoot) {
      return;
    }
    const data = this._data;
    const tabs = this._tabs()
      .map(
        ([key, label]) =>
          `<button role="tab" class="tab ${key === this._tab ? "active" : ""}" data-tab="${key}" aria-selected="${key === this._tab}">${label}</button>`
      )
      .join("");
    let body = "";
    if (this._error) {
      body = `<p class="error" role="alert">Не удалось загрузить данные: ${this._escape(this._error)}</p>`;
    } else if (!data) {
      body = `<p class="muted">Загрузка…</p>`;
    } else {
      body = this._renderTab(data);
    }
    this.shadowRoot.innerHTML = `
      <style>
        :host { display: block; padding: 16px; color: var(--primary-text-color); }
        h1 { font-size: 1.4rem; margin: 0 0 12px; }
        .tabs { display: flex; flex-wrap: wrap; gap: 4px; border-bottom: 1px solid var(--divider-color); margin-bottom: 16px; }
        .tab { font: inherit; padding: 8px 12px; border: none; border-bottom: 2px solid transparent; background: transparent; color: var(--secondary-text-color); cursor: pointer; border-radius: 6px 6px 0 0; transition: background-color 150ms ease, color 150ms ease; }
        .tab:hover { background: var(--secondary-background-color); }
        .tab:focus-visible { outline: 2px solid var(--primary-color); outline-offset: 2px; }
        .tab.active { color: var(--primary-text-color); border-bottom-color: var(--primary-color); }
        table { width: 100%; border-collapse: collapse; font-size: 0.95rem; }
        th, td { text-align: left; padding: 6px 8px; border-bottom: 1px solid var(--divider-color); vertical-align: top; }
        th { color: var(--secondary-text-color); font-weight: 500; }
        .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 12px; }
        .card { background: var(--card-background-color); border-radius: 12px; padding: 12px 16px; box-shadow: var(--ha-card-box-shadow, 0 1px 3px rgba(0,0,0,0.12)); }
        .card .value { font-size: 1.6rem; font-weight: 600; }
        .muted { color: var(--secondary-text-color); }
        .error { color: var(--error-color); }
        .badge { display: inline-block; padding: 2px 8px; border-radius: 999px; font-size: 0.8rem; background: var(--secondary-background-color); }
        .critical { background: var(--error-color); color: #fff; }
        .warning { background: var(--warning-color, #ffa600); color: #1b1b1b; }
        @media (max-width: 600px) { :host { padding: 8px; } th, td { padding: 4px; } }
      </style>
      <h1>HA Smart Analyst</h1>
      <div class="tabs" role="tablist">${tabs}</div>
      <div role="tabpanel">${body}</div>
    `;
    this.shadowRoot.querySelectorAll(".tab").forEach((button) => {
      button.addEventListener("click", () => {
        this._tab = button.dataset.tab;
        this._render();
      });
      button.addEventListener("keydown", (event) => {
        if (event.key === "Enter" || event.key === " ") {
          event.preventDefault();
          button.click();
        }
      });
    });
  }

  _renderTab(data) {
    switch (this._tab) {
      case "overview":
        return this._overview(data);
      case "systems":
        return this._systems(data);
      case "links":
        return this._table(
          ["Связь", "Тип", "Источник", "Цель"],
          data.relationships.map((item) => [item.id, item.type, item.source_id, item.target_id])
        );
      case "rules":
        return this._table(
          ["Правило", "Критичность", "Состояние", "Включено", "Политика данных"],
          data.rules.map((rule) => [rule.name, rule.severity, rule.state, rule.enabled ? "да" : "нет", rule.missing_data_policy])
        );
      case "incidents":
        return data.incidents.length
          ? this._table(
              ["Правило", "Критичность", "Состояние", "Открыт", "Пик", "Принято"],
              data.incidents.map((item) => [
                item.rule,
                item.severity,
                item.state,
                item.opened_at,
                item.peak_deviation === null ? "—" : `${item.peak_deviation} ${item.peak_unit || ""}`,
                item.acknowledged ? "да" : "нет",
              ])
            )
          : `<p class="muted">Активных инцидентов нет.</p>`;
      case "reports":
        return data.reports.length
          ? this._table(
              ["Профиль", "Детализация", "Язык", "Тип"],
              data.reports.map((item) => [item.name, item.detail, item.locale, item.report_type])
            )
          : `<p class="muted">Профили отчётов ещё не созданы.</p>`;
      case "ai":
        return `<div class="grid">
          <div class="card"><div class="muted">Провайдер</div><div class="value">${this._escape(data.ai.model || "—")}</div></div>
          <div class="card"><div class="muted">Доступен</div><div class="value">${data.ai.available ? "да" : "нет"}</div></div>
          <div class="card"><div class="muted">Ошибка</div><div>${this._escape(data.ai.error || "—")}</div></div>
        </div>`;
      case "telegram":
        return `<div class="grid">
          <div class="card"><div class="muted">Служба notify</div><div class="value">${this._escape(data.telegram.service || "—")}</div></div>
          <div class="card"><div class="muted">Получатели</div><div>${this._escape((data.telegram.recipients || []).join(", ") || "—")}</div></div>
        </div>`;
      case "history":
        return `<p>${data.history.available ? "Recorder доступен." : "История недоступна — отчёты не будут утверждать тренды."}</p>`;
      case "diagnostics":
        return this._table(
          ["Показатель", "Значение"],
          [
            ["Измерений", data.diagnostics.measurements_total],
            ["Устаревших источников", data.diagnostics.stale_sources],
            ["Очередь сообщений", data.overview.queue_pending],
            ["Токенов израсходовано", data.overview.tokens_used],
            ["Сформировано", data.overview.generated_at || "—"],
          ]
        );
      default:
        return "";
    }
  }

  _overview(data) {
    const overview = data.overview;
    return `<div class="grid">
      <div class="card"><div class="muted">Активные инциденты</div><div class="value">${overview.active_incidents}</div></div>
      <div class="card"><div class="muted">Критические</div><div class="value">${overview.critical_incidents}</div></div>
      <div class="card"><div class="muted">Правила</div><div class="value">${overview.rules_active} / ${overview.rules_total}</div></div>
      <div class="card"><div class="muted">Очередь</div><div class="value">${overview.queue_pending}</div></div>
      <div class="card"><div class="muted">Устаревшие источники</div><div class="value">${overview.stale_sources}</div></div>
    </div>`;
  }

  _systems(data) {
    if (!data.systems.length) {
      return `<p class="muted">Системы не созданы. Добавьте их в настройках интеграции.</p>`;
    }
    return data.systems
      .map(
        (system) => `<h2>${this._escape(system.name)}</h2>${this._table(
          ["Измерение", "Роль", "Значение", "Качество", "Сущность"],
          system.measurements.map((item) => [
            item.name,
            item.role,
            item.value === null ? "—" : `${item.value} ${item.unit || ""}`,
            item.quality,
            item.entity_id || "—",
          ])
        )}`
      )
      .join("");
  }

  _table(headers, rows) {
    const head = headers.map((header) => `<th scope="col">${this._escape(header)}</th>`).join("");
    const body = rows
      .map(
        (row) =>
          `<tr>${row
            .map((cell) => `<td>${this._escape(cell === null || cell === undefined ? "—" : String(cell))}</td>`)
            .join("")}</tr>`
      )
      .join("");
    return `<table><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table>`;
  }

  _escape(value) {
    return String(value)
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;");
  }
}

customElements.define("ha-smart-analyst-panel", HaSmartAnalystPanel);
