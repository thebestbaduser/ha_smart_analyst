const ROLES = [
  ["custom", "custom"],
  ["actual", "actual"],
  ["setpoint", "setpoint"],
  ["supply", "supply"],
  ["return", "return"],
  ["power", "power"],
  ["energy", "energy"],
  ["voltage_phase", "voltage_phase"],
  ["mode", "mode"],
  ["running", "running"],
  ["fault", "fault"],
  ["flow", "flow"],
  ["pressure", "pressure"],
  ["temperature_indoor", "temperature_indoor"],
  ["temperature_outdoor", "temperature_outdoor"],
  ["occupancy", "occupancy"],
  ["leak", "leak"],
];

const PRESETS = [
  ["custom", "Пользовательская"],
  ["heating", "Отопление"],
  ["dhw", "ГВС"],
  ["floor_heating", "Тёплый пол"],
  ["radiators", "Радиаторы"],
  ["electricity", "Электрика"],
  ["energy", "Энергоучёт"],
  ["water", "Вода"],
  ["climate", "Климат"],
  ["ventilation", "Вентиляция"],
  ["security", "Безопасность"],
  ["lighting", "Освещение"],
];

const OPERATORS = [
  ["gt", "больше"],
  ["ge", "больше или равно"],
  ["lt", "меньше"],
  ["le", "меньше или равно"],
  ["eq", "равно"],
  ["ne", "не равно"],
];

const SEVERITIES = [
  ["warning", "предупреждение"],
  ["critical", "критично"],
  ["info", "информация"],
];

class HaSmartAnalystPanel extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._tab = "overview";
    this._data = null;
    this._error = null;
    this._flash = null;
    this._dialog = null;
    this._dialogSystemId = null;
    this._entities = null;
    this._entitiesLoading = false;
    this._entityQuery = "";
    this._reportText = "";
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

  // -- данные ---------------------------------------------------------
  async _load() {
    if (!this.hass) {
      return;
    }
    try {
      this._data = await this.hass.callWS({ type: "ha_smart_analyst/overview" });
      this._error = null;
    } catch (err) {
      this._error = this._message(err);
    }
    this._render();
  }

  async _loadEntities() {
    if (!this.hass) {
      return;
    }
    this._entitiesLoading = true;
    this._render();
    try {
      this._entities = await this.hass.callWS({
        type: "ha_smart_analyst/entities",
        query: this._entityQuery || undefined,
        only_unused: true,
        limit: 100,
      });
    } catch (err) {
      this._flash = { type: "error", text: this._message(err) };
      this._entities = { entities: [], total: 0, unavailable: 0 };
    }
    this._entitiesLoading = false;
    this._render();
  }

  _message(err) {
    if (!err) {
      return "неизвестная ошибка";
    }
    if (err.message) {
      return err.message;
    }
    if (err.error && err.error.message) {
      return err.error.message;
    }
    return String(err);
  }

  _ok(text) {
    this._flash = { type: "ok", text };
    this._render();
  }

  _fail(err) {
    this._flash = { type: "error", text: this._message(err) };
    this._render();
  }

  // -- действия -------------------------------------------------------
  _open(dialog, systemId = null) {
    this._dialog = dialog;
    this._dialogSystemId = systemId;
    this._flash = null;
    if (dialog === "entity") {
      this._entities = null;
      this._loadEntities();
    } else {
      this._render();
    }
  }

  _close() {
    this._dialog = null;
    this._dialogSystemId = null;
    this._entities = null;
    this._render();
  }

  async _call(type, payload) {
    return this.hass.callWS({ type: `ha_smart_analyst/${type}`, ...payload });
  }

  async _submitSystem() {
    const name = this.shadowRoot.querySelector("#system-name").value.trim();
    const preset = this.shadowRoot.querySelector("#system-preset").value;
    try {
      await this._call("system/create", { name, preset });
      this._dialog = null;
      await this._load();
      this._ok(`Система «${name}» добавлена`);
    } catch (err) {
      this._fail(err);
    }
  }

  async _deleteSystem(systemId) {
    if (!window.confirm("Удалить систему вместе с её датчиками? Сущности Home Assistant останутся.")) {
      return;
    }
    try {
      await this._call("system/delete", { system_id: systemId });
      await this._load();
      this._ok("Система удалена");
    } catch (err) {
      this._fail(err);
    }
  }

  async _addMeasurement(button) {
    const dataset = button.dataset;
    try {
      await this._call("measurement/add", {
        system_id: this._dialogSystemId,
        entity_id: dataset.entity,
        name: dataset.name || undefined,
        role: dataset.role || "custom",
        unit: dataset.unit || undefined,
        registry_entry_id: dataset.registry || undefined,
      });
      this._ok(`Датчик «${dataset.name || dataset.entity}» добавлен`);
      await this._load();
      await this._loadEntities();
    } catch (err) {
      this._fail(err);
    }
  }

  async _deleteMeasurement(measurementId) {
    if (!window.confirm("Удалить датчик из системы?")) {
      return;
    }
    try {
      await this._call("measurement/delete", { measurement_id: measurementId });
      await this._load();
      this._ok("Датчик удалён");
    } catch (err) {
      this._fail(err);
    }
  }

  async _submitRule() {
    const root = this.shadowRoot;
    const name = root.querySelector("#rule-name").value.trim();
    const severity = root.querySelector("#rule-severity").value;
    const measurementId = root.querySelector("#rule-measurement").value;
    const op = root.querySelector("#rule-op").value;
    const threshold = parseFloat(root.querySelector("#rule-threshold").value);
    const forSeconds = parseInt(root.querySelector("#rule-for").value || "0", 10);
    const clearValue = root.querySelector("#rule-clear").value;
    const stableSeconds = parseInt(root.querySelector("#rule-stable").value || "0", 10);
    const measurement = this._findMeasurement(measurementId);
    const unit = measurement ? measurement.unit : undefined;
    const expression = {
      op,
      left: { measurement_id: measurementId },
      right: { value: threshold, unit: unit || undefined },
    };
    const payload = {
      id: `rule_${Math.random().toString(36).slice(2, 10)}`,
      name,
      severity,
      expression,
      for_seconds: Number.isFinite(forSeconds) ? forSeconds : 0,
      scope_id: measurement ? measurement.system_id : undefined,
      missing_data_policy: "unknown",
    };
    if (!Number.isFinite(threshold)) {
      this._fail("порог должен быть числом");
      return;
    }
    if (clearValue !== "" && Number.isFinite(parseFloat(clearValue))) {
      const recovery = {
        stable_seconds: Number.isFinite(stableSeconds) ? stableSeconds : 0,
        measurement_id: measurementId,
      };
      if (op === "gt" || op === "ge") {
        recovery.clear_below = { value: parseFloat(clearValue), unit: unit || undefined };
      } else if (op === "lt" || op === "le") {
        recovery.clear_above = { value: parseFloat(clearValue), unit: unit || undefined };
      }
      if (recovery.clear_below || recovery.clear_above) {
        payload.recovery = recovery;
      }
    }
    try {
      await this._call("rule/save", { rule: payload });
      this._dialog = null;
      await this._load();
      this._ok(`Правило «${name}» сохранено`);
    } catch (err) {
      this._fail(err);
    }
  }

  async _testRule(ruleId) {
    try {
      const result = await this._call("rule_test", { rule_id: ruleId });
      const value = result.value === null ? "UNKNOWN" : String(result.value);
      const reasons = (result.reasons || []).map((item) => item.message).join("; ");
      this._ok(`Результат: ${value}${reasons ? " — " + reasons : ""}`);
    } catch (err) {
      this._fail(err);
    }
  }

  async _deleteRule(ruleId) {
    if (!window.confirm("Удалить правило? Журнал инцидентов останется.")) {
      return;
    }
    try {
      await this._call("rule/delete", { rule_id: ruleId });
      await this._load();
      this._ok("Правило удалено");
    } catch (err) {
      this._fail(err);
    }
  }

  async _submitProfile() {
    const root = this.shadowRoot;
    const name = root.querySelector("#profile-name").value.trim();
    const locale = root.querySelector("#profile-locale").value;
    const detail = root.querySelector("#profile-detail").value;
    const timesRaw = root.querySelector("#profile-times").value.trim();
    const scheduleTimes = timesRaw
      ? timesRaw.split(",").map((item) => item.trim()).filter(Boolean)
      : [];
    const days = root.querySelector("#profile-days").value;
    const weekdays = days === "weekdays" ? [0, 1, 2, 3, 4] : days === "weekends" ? [5, 6] : [];
    try {
      await this._call("profile/save", {
        profile: {
          id: `profile_${Math.random().toString(36).slice(2, 10)}`,
          name,
          locale,
          detail,
          timezone: this.hass.config.time_zone,
          report_type: scheduleTimes.length ? "scheduled" : "on_demand",
          schedule_times: scheduleTimes,
          schedule_weekdays: weekdays,
        },
      });
      this._dialog = null;
      await this._load();
      this._ok(`Профиль «${name}» сохранён`);
    } catch (err) {
      this._fail(err);
    }
  }

  async _deleteProfile(profileId) {
    if (!window.confirm("Удалить профиль отчёта? Уже отправленные отчёты останутся.")) {
      return;
    }
    try {
      await this._call("profile/delete", { profile_id: profileId });
      await this._load();
      this._ok("Профиль удалён");
    } catch (err) {
      this._fail(err);
    }
  }

  async _generateReport(profileId) {
    try {
      const response = await this.hass.callService(
        "ha_smart_analyst",
        "generate_report",
        { profile_id: profileId },
        undefined,
        false,
        true
      );
      const payload = this._servicePayload(response);
      const text = payload.text || "";
      if (!text) {
        this._fail(
          "Сервис вернул пустой отчёт: " + JSON.stringify(response || {}).slice(0, 300)
        );
        return;
      }
      this._reportText = text;
      this._open("report");
    } catch (err) {
      this._fail(err);
    }
  }

  _servicePayload(response) {
    if (!response) {
      return {};
    }
    if (typeof response === "string") {
      return { text: response };
    }
    if (typeof response.text === "string") {
      return response;
    }
    if (response.response && typeof response.response === "object") {
      return response.response;
    }
    if (response.service_response && typeof response.service_response === "object") {
      return response.service_response;
    }
    return response;
  }

  async _acknowledge(incidentId) {
    try {
      await this.hass.callService("ha_smart_analyst", "acknowledge_incident", {
        incident_id: incidentId,
        by: "panel",
      });
      await this._load();
      this._ok("Инцидент принят; он остаётся активным до восстановления");
    } catch (err) {
      this._fail(err);
    }
  }

  async _testNotify() {
    try {
      const response = await this.hass.callService(
        "ha_smart_analyst",
        "test_notification",
        { message: "Проверка уведомлений HA Smart Analyst" },
        undefined,
        false,
        true
      );
      const payload = this._servicePayload(response);
      const results = payload.results || [];
      const failed = results.filter((item) => !item.sent);
      if (failed.length) {
        this._fail(
          "Не доставлено: " + failed.map((item) => `${item.target}: ${item.detail}`).join("; ")
        );
        return;
      }
      this._ok(
        "Отправлено: " +
          results.map((item) => item.target).join(", ") +
          " (принято службой notify; доставку в Telegram контролирует интеграция Telegram)"
      );
    } catch (err) {
      this._fail(err);
    }
  }

  _findMeasurement(measurementId) {
    if (!this._data) {
      return null;
    }
    for (const system of this._data.systems) {
      for (const measurement of system.measurements) {
        if (measurement.id === measurementId) {
          return { ...measurement, system_id: system.id };
        }
      }
    }
    return null;
  }

  // -- отрисовка ------------------------------------------------------
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
    const data = this._data;
    const tabs = this._tabs()
      .map(
        ([key, label]) =>
          `<button role="tab" class="tab ${key === this._tab ? "active" : ""}" data-tab="${key}" aria-selected="${key === this._tab}">${label}</button>`
      )
      .join("");
    let body = "";
    if (this._dialog) {
      body = this._renderDialog();
    } else if (this._error) {
      body = `<p class="error" role="alert">Не удалось загрузить данные: ${this._escape(this._error)}</p>`;
    } else if (!data) {
      body = `<p class="muted">Загрузка…</p>`;
    } else {
      body = this._renderTab(data);
    }
    const flash = this._flash
      ? `<p class="${this._flash.type === "error" ? "error" : "ok"}" role="status">${this._escape(this._flash.text)}</p>`
      : "";
    this.shadowRoot.innerHTML = `
      <style>
        :host { display: block; padding: 16px; color: var(--primary-text-color); }
        h1 { font-size: 1.4rem; margin: 0 0 12px; }
        h2 { font-size: 1.1rem; margin: 16px 0 8px; }
        .tabs { display: flex; flex-wrap: wrap; gap: 4px; border-bottom: 1px solid var(--divider-color); margin-bottom: 12px; }
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
        .ok { color: var(--success-color, #0a7d33); }
        .row { display: flex; flex-wrap: wrap; gap: 8px; align-items: end; margin: 8px 0; }
        label { display: flex; flex-direction: column; gap: 4px; font-size: 0.85rem; color: var(--secondary-text-color); }
        input, select, textarea { font: inherit; padding: 6px 8px; border: 1px solid var(--divider-color); border-radius: 6px; background: var(--card-background-color); color: var(--primary-text-color); min-width: 120px; }
        input:focus-visible, select:focus-visible, textarea:focus-visible { outline: 2px solid var(--primary-color); outline-offset: 1px; }
        button { font: inherit; padding: 6px 12px; border-radius: 6px; border: 1px solid var(--divider-color); background: var(--card-background-color); color: var(--primary-text-color); cursor: pointer; transition: background-color 150ms ease; }
        button:hover { background: var(--secondary-background-color); }
        button:focus-visible { outline: 2px solid var(--primary-color); outline-offset: 2px; }
        button.primary { background: var(--primary-color); color: var(--text-primary-color, #fff); border-color: var(--primary-color); }
        button.danger { color: var(--error-color); }
        .chip { display: inline-block; padding: 2px 8px; border-radius: 999px; font-size: 0.8rem; background: var(--secondary-background-color); margin-right: 4px; }
        .chip.high { background: var(--primary-color); color: var(--text-primary-color, #fff); }
        pre { white-space: pre-wrap; font-family: inherit; background: var(--secondary-background-color); padding: 12px; border-radius: 8px; }
        @media (max-width: 600px) { :host { padding: 8px; } th, td { padding: 4px; } }
      </style>
      <h1>HA Smart Analyst${
        data && data.version ? ` <span class="muted">v${this._escape(data.version)}</span>` : ""
      }</h1>
      <div class="tabs" role="tablist">${tabs}</div>
      ${flash}
      <div role="tabpanel">${body}</div>
    `;
    this._bind();
  }

  _renderDialog() {
    if (this._dialog === "system") {
      return `<div class="card">
        <h2>Новая система</h2>
        <div class="row">
          <label>Название<input id="system-name" value="Отопление"></label>
          <label>Тип<select id="system-preset">${PRESETS.map(
            ([value, label]) => `<option value="${value}"${value === "heating" ? " selected" : ""}>${label}</option>`
          ).join("")}</select></label>
          <button class="primary" data-action="submit-system">Создать</button>
          <button data-action="close">Отмена</button>
        </div>
      </div>`;
    }
    if (this._dialog === "entity") {
      return this._renderPicker();
    }
    if (this._dialog === "rule") {
      return this._renderRuleDialog();
    }
    if (this._dialog === "profile") {
      return `<div class="card">
        <h2>Новый профиль отчёта</h2>
        <div class="row">
          <label>Название<input id="profile-name" value="Отчёт"></label>
          <label>Язык<select id="profile-locale"><option value="ru">ru</option><option value="en">en</option></select></label>
          <label>Детализация<select id="profile-detail">
            <option value="adaptive">adaptive</option>
            <option value="compact">compact</option>
            <option value="standard">standard</option>
            <option value="detailed">detailed</option>
          </select></label>
        </div>
        <div class="row">
          <label>Время (HH:MM, через запятую)<input id="profile-times" placeholder="08:00, 20:00"></label>
          <label>Дни<select id="profile-days">
            <option value="every">каждый день</option>
            <option value="weekdays">по будням</option>
            <option value="weekends">по выходным</option>
          </select></label>
          <button class="primary" data-action="submit-profile">Сохранить</button>
          <button data-action="close">Отмена</button>
        </div>
        <p class="muted">Пустое время — отчёт только по кнопке. Время указывается в часовом поясе Home Assistant.</p>
      </div>`;
    }
    if (this._dialog === "report") {
      return `<div class="card">
        <h2>Отчёт</h2>
        <pre>${this._escape(this._reportText)}</pre>
        <button data-action="close">Закрыть</button>
      </div>`;
    }
    return "";
  }

  _renderPicker() {
    if (this._entitiesLoading || this._entities === null) {
      return `<div class="card"><p class="muted">Загрузка каталога…</p><button data-action="close">Отмена</button></div>`;
    }
    const entities = this._entities.entities || [];
    const rows = entities
      .map((item) => {
        const suggestion = item.suggestion || {};
        const high = (suggestion.confidence || 0) >= 0.6;
        return `<tr>
          <td>${this._escape(item.name)}<br><span class="muted">${this._escape(item.entity_id)}</span></td>
          <td>${this._escape(item.unit || "—")}</td>
          <td><span class="chip ${high ? "high" : ""}">${this._escape(suggestion.role || "custom")}</span>
              <span class="chip">${this._escape(suggestion.preset || "custom")}</span>
              ${Math.round((suggestion.confidence || 0) * 100)}%</td>
          <td class="muted">${this._escape(suggestion.reason || "")}</td>
          <td><button class="primary" data-action="add-measurement"
              data-entity="${this._escape(item.entity_id)}"
              data-name="${this._escape(suggestion.suggested_name || item.name)}"
              data-role="${this._escape(suggestion.role || "custom")}"
              data-unit="${this._escape(item.unit || "")}"
              data-registry="${this._escape(item.registry_entry_id || "")}">Добавить</button></td>
        </tr>`;
      })
      .join("");
    return `<div class="card">
      <h2>Добавить датчик</h2>
      <div class="row">
        <label>Поиск<input id="entity-filter" value="${this._escape(this._entityQuery)}" placeholder="имя, entity_id, устройство"></label>
        <button data-action="filter-entities">Найти</button>
        <button data-action="close">Готово</button>
      </div>
      <p class="muted">Найдено: ${entities.length} из ${this._entities.total || 0}. Подсказка — предположение, проверьте роль перед добавлением.</p>
      <table><thead><tr><th>Сущность</th><th>Ед.</th><th>Подсказка</th><th>Причина</th><th></th></tr></thead>
      <tbody>${rows || '<tr><td colspan="5" class="muted">Ничего не найдено</td></tr>'}</tbody></table>
    </div>`;
  }

  _renderRuleDialog() {
    const options = [];
    for (const system of this._data ? this._data.systems : []) {
      for (const measurement of system.measurements) {
        options.push(
          `<option value="${this._escape(measurement.id)}" data-unit="${this._escape(measurement.unit || "")}">
            ${this._escape(system.name)} → ${this._escape(measurement.name)} (${this._escape(measurement.unit || "—")})
          </option>`
        );
      }
    }
    if (!options.length) {
      return `<div class="card"><p class="muted">Сначала добавьте хотя бы один датчик.</p><button data-action="close">Закрыть</button></div>`;
    }
    return `<div class="card">
      <h2>Новое правило</h2>
      <div class="row">
        <label>Название<input id="rule-name" value="Порог датчика"></label>
        <label>Критичность<select id="rule-severity">${SEVERITIES.map(
          ([value, label]) => `<option value="${value}">${label}</option>`
        ).join("")}</select></label>
      </div>
      <div class="row">
        <label>Датчик<select id="rule-measurement">${options.join("")}</select></label>
        <label>Условие<select id="rule-op">${OPERATORS.map(
          ([value, label]) => `<option value="${value}">${label}</option>`
        ).join("")}</select></label>
        <label>Порог<input id="rule-threshold" type="number" step="any" value="245"></label>
        <label>Держать, с<input id="rule-for" type="number" min="0" value="30"></label>
      </div>
      <div class="row">
        <label>Порог восстановления<input id="rule-clear" type="number" step="any" placeholder="необязательно"></label>
        <label>Стабильно, с<input id="rule-stable" type="number" min="0" value="60"></label>
        <button class="primary" data-action="submit-rule">Сохранить</button>
        <button data-action="close">Отмена</button>
      </div>
      <p class="muted">Пороги задаёте вы: значения по умолчанию — только пример, не рекомендация по безопасности.</p>
    </div>`;
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
        return this._rules(data);
      case "incidents":
        return this._incidents(data);
      case "reports":
        return this._reports(data);
      case "ai":
        return `<div class="grid">
          <div class="card"><div class="muted">Провайдер</div><div class="value">${this._escape(data.ai.model || "—")}</div></div>
          <div class="card"><div class="muted">Доступен</div><div class="value">${data.ai.available ? "да" : "нет"}</div></div>
          <div class="card"><div class="muted">Ошибка</div><div>${this._escape(data.ai.error || "—")}</div></div>
        </div>`;
      case "telegram":
        return `<div class="grid">
          <div class="card"><div class="muted">Notify-сущность</div><div class="value">${this._escape(data.telegram.service || "—")}</div></div>
          <div class="card"><div class="muted">Куда доставлять</div><div>${this._escape((data.telegram.targets || []).join(", ") || "—")}</div></div>
        </div>
        <p class="muted">Если выбрана notify-сущность Telegram, chat_id не нужен. Если ничего не выбрано — уведомления остаются внутри Home Assistant.</p>
        <div class="row"><button class="primary" data-action="test-notify">Отправить проверочное сообщение</button></div>`;
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
    const add = `<div class="row"><button class="primary" data-action="new-system">Добавить систему</button></div>`;
    if (!data.systems.length) {
      return `${add}<p class="muted">Систем пока нет. Начните с «Отопление» или «Электрика».</p>`;
    }
    return (
      add +
      data.systems
        .map(
          (system) => `<h2>${this._escape(system.name)}
            <button data-action="add-entity" data-system="${this._escape(system.id)}">Добавить датчик</button>
            <button class="danger" data-action="delete-system" data-system="${this._escape(system.id)}">Удалить систему</button>
          </h2>${this._table(
            ["Измерение", "Роль", "Значение", "Качество", "Сущность", ""],
            system.measurements.map((item) => [
              item.name,
              item.role,
              item.value === null ? "—" : `${item.value} ${item.unit || ""}`,
              item.quality,
              item.entity_id || "—",
              `<button class="danger" data-action="delete-measurement" data-measurement="${this._escape(item.id)}">Удалить</button>`,
            ])
          )}`
        )
        .join("")
    );
  }

  _rules(data) {
    const add = `<div class="row"><button class="primary" data-action="new-rule">Создать правило</button></div>`;
    if (!data.rules.length) {
      return `${add}<p class="muted">Правил нет. Правило — это условие по датчику с порогом и длительностью.</p>`;
    }
    return (
      add +
      this._table(
        ["Правило", "Критичность", "Состояние", "Держать, с", "Политика данных", ""],
        data.rules.map((rule) => [
          rule.name,
          rule.severity,
          rule.state,
          rule.for_seconds,
          rule.missing_data_policy,
          `<button data-action="test-rule" data-rule="${this._escape(rule.id)}">Тест</button>
           <button class="danger" data-action="delete-rule" data-rule="${this._escape(rule.id)}">Удалить</button>`,
        ])
      )
    );
  }

  _incidents(data) {
    if (!data.incidents.length) {
      return `<p class="muted">Инцидентов нет.</p>`;
    }
    return this._table(
      ["Правило", "Критичность", "Состояние", "Открыт", "Пик", "Принято", ""],
      data.incidents.map((item) => [
        item.rule,
        item.severity,
        item.state,
        item.opened_at,
        item.peak_deviation === null ? "—" : `${item.peak_deviation} ${item.peak_unit || ""}`,
        item.acknowledged ? "да" : "нет",
        item.acknowledged
          ? ""
          : `<button data-action="ack" data-incident="${this._escape(item.incident_id)}">Принять</button>`,
      ])
    );
  }

  _reports(data) {
    const add = `<div class="row"><button class="primary" data-action="new-profile">Новый профиль</button></div>`;
    if (!data.reports.length) {
      return `${add}<p class="muted">Профилей нет. Профиль задаёт время, язык и детализацию отчёта.</p>`;
    }
    return (
      add +
      this._table(
        ["Профиль", "Расписание", "Следующий запуск", "Детализация", "Язык", ""],
        data.reports.map((item) => [
          item.name,
          item.schedule || "вручную",
          item.next_run ? new Date(item.next_run).toLocaleString() : "—",
          item.detail,
          item.locale,
          `<button class="primary" data-action="generate-report" data-profile="${this._escape(item.id)}">Сформировать</button>
           <button class="danger" data-action="delete-profile" data-profile="${this._escape(item.id)}">Удалить</button>`,
        ])
      )
    );
  }

  _table(headers, rows) {
    const head = headers.map((header) => `<th scope="col">${this._escape(header)}</th>`).join("");
    const body = rows
      .map((row) => {
        const cells = Array.isArray(row) ? row : [row];
        return `<tr>${cells
          .map((cell) => {
            if (typeof cell === "string" && cell.startsWith("<button")) {
              return `<td>${cell}</td>`;
            }
            return `<td>${this._escape(cell === null || cell === undefined ? "—" : String(cell))}</td>`;
          })
          .join("")}</tr>`;
      })
      .join("");
    return `<table><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table>`;
  }

  _bind() {
    const root = this.shadowRoot;
    root.querySelectorAll(".tab").forEach((button) => {
      button.addEventListener("click", () => {
        this._tab = button.dataset.tab;
        this._dialog = null;
        this._render();
      });
      button.addEventListener("keydown", (event) => {
        if (event.key === "Enter" || event.key === " ") {
          event.preventDefault();
          button.click();
        }
      });
    });
    root.querySelectorAll("[data-action]").forEach((element) => {
      element.addEventListener("click", () => this._dispatch(element.dataset, element));
    });
    const filter = root.querySelector("#entity-filter");
    if (filter) {
      filter.addEventListener("keydown", (event) => {
        if (event.key === "Enter") {
          event.preventDefault();
          this._entityQuery = filter.value;
          this._loadEntities();
        }
      });
    }
  }

  _dispatch(dataset, element) {
    switch (dataset.action) {
      case "new-system":
        this._open("system");
        break;
      case "close":
        this._close();
        break;
      case "submit-system":
        this._submitSystem();
        break;
      case "add-entity":
        this._open("entity", dataset.system);
        break;
      case "delete-system":
        this._deleteSystem(dataset.system);
        break;
      case "add-measurement":
        this._addMeasurement(element);
        break;
      case "delete-measurement":
        this._deleteMeasurement(dataset.measurement);
        break;
      case "filter-entities": {
        const input = this.shadowRoot.querySelector("#entity-filter");
        this._entityQuery = input ? input.value : "";
        this._loadEntities();
        break;
      }
      case "new-rule":
        this._open("rule");
        break;
      case "submit-rule":
        this._submitRule();
        break;
      case "test-rule":
        this._testRule(dataset.rule);
        break;
      case "delete-rule":
        this._deleteRule(dataset.rule);
        break;
      case "new-profile":
        this._open("profile");
        break;
      case "submit-profile":
        this._submitProfile();
        break;
      case "generate-report":
        this._generateReport(dataset.profile);
        break;
      case "delete-profile":
        this._deleteProfile(dataset.profile);
        break;
      case "ack":
        this._acknowledge(dataset.incident);
        break;
      case "test-notify":
        this._testNotify();
        break;
      default:
        break;
    }
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
