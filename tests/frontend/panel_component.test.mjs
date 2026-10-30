import assert from "node:assert/strict";
import test from "node:test";

class FakeShadowRoot {
  constructor() {
    this.innerHTML = "";
  }

  querySelectorAll() {
    return [];
  }

  querySelector() {
    return null;
  }
}

class FakeHTMLElement {
  constructor() {
    this._shadow = new FakeShadowRoot();
  }

  attachShadow() {
    return this._shadow;
  }

  get shadowRoot() {
    return this._shadow;
  }
}

globalThis.HTMLElement = FakeHTMLElement;
globalThis.customElements = { define() {} };
globalThis.window = { confirm: () => true };

const FRONTEND_VERSION = "0.3.5";
const panelUrl = new URL(
  `../../custom_components/ha_smart_analyst/frontend/ha-smart-analyst-panel.js?v=${FRONTEND_VERSION}`,
  import.meta.url
);
const { HaSmartAnalystPanel } = await import(panelUrl.href);

function makePanel(callService, backendVersion = FRONTEND_VERSION) {
  const panel = new HaSmartAnalystPanel();
  panel._data = {
    version: backendVersion,
    overview: {
      active_incidents: 0,
      critical_incidents: 0,
      rules_active: 0,
      rules_total: 0,
      queue_pending: 0,
      stale_sources: 0,
    },
    reports: [],
    systems: [],
    rules: [],
    incidents: [],
    relationships: [],
  };
  panel._hass = {
    config: { time_zone: "UTC" },
    callService: async (...args) => {
      assert.equal(args[0], "ha_smart_analyst");
      assert.equal(args[1], "generate_report");
      return callService();
    },
  };
  return panel;
}

test("панель показывает отчёт и источник локальных фактов", async () => {
  const panel = makePanel(async () => ({
    response: { text: "**Отчёт**\n57,8 °C", used_ai: false, detail: "compact" },
  }));

  await panel._generateReport("p1");

  const html = panel.shadowRoot.innerHTML;
  assert.match(html, /Источник: локальные факты/);
  assert.match(html, /\*\*Отчёт\*\*/);
  assert.match(html, /57,8/);
  assert.doesNotMatch(html, /undefined/);
});

test("панель разворачивает вложенный ответ и отмечает ИИ", async () => {
  const panel = makePanel(async () => ({
    service_response: { text: "Вывод модели", used_ai: true, detail: "standard" },
  }));

  await panel._generateReport("p2");

  const html = panel.shadowRoot.innerHTML;
  assert.match(html, /Источник: заключение DeepSeek/);
  assert.match(html, /Вывод модели/);
});

test("пустой ответ даёт диагностику, а не окно с undefined", async () => {
  const panel = makePanel(async () => ({}));

  await panel._generateReport("p3");

  const html = panel.shadowRoot.innerHTML;
  assert.match(html, /не вернул текст отчёта/);
  assert.match(html, /этап: ответ service/);
  assert.match(html, /frontend 0\.3\.5/);
  assert.doesNotMatch(html, /undefined/);
  assert.equal(panel._dialog, null);
});

test("ошибка сервиса объясняет причину", async () => {
  const panel = makePanel(async () => {
    throw new Error("HTTP 500: сервис недоступен");
  });

  await panel._generateReport("p4");

  const html = panel.shadowRoot.innerHTML;
  assert.match(html, /HTTP 500: сервис недоступен/);
  assert.doesNotMatch(html, /undefined/);
});

test("несовпадение версий frontend и backend видно в шапке", () => {
  const stale = makePanel(async () => ({}), "0.3.4");
  stale._render();
  assert.match(stale.shadowRoot.innerHTML, /не совпадает с backend v0\.3\.4/);

  const fresh = makePanel(async () => ({}), FRONTEND_VERSION);
  fresh._render();
  assert.doesNotMatch(fresh.shadowRoot.innerHTML, /не совпадает/);
});
