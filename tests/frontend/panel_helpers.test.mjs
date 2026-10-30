import assert from "node:assert/strict";
import test from "node:test";

import {
  errorMessage,
  parseSchedule,
  profileDraft,
  reportMessage,
  servicePayload,
} from "../../custom_components/ha_smart_analyst/frontend/panel-helpers.js";

test("успешный ответ сервиса читается напрямую", () => {
  const message = reportMessage({ text: "Отчёт", used_ai: true, detail: "compact" });
  assert.equal(message.text, "Отчёт");
  assert.equal(message.usedAi, true);
  assert.equal(message.detail, "compact");
});

test("вложенный ответ Home Assistant разворачивается", () => {
  assert.equal(servicePayload({ response: { text: "Из response" } }).text, "Из response");
  assert.equal(
    servicePayload({ service_response: { text: "Из service_response" } }).text,
    "Из service_response"
  );
  assert.equal(reportMessage({ response: { text: "  Отчёт  " } }).text, "Отчёт");
});

test("пустой ответ не превращается в undefined", () => {
  assert.equal(reportMessage({}), null);
  assert.equal(reportMessage(null), null);
  assert.equal(reportMessage({ text: "   " }), null);
  assert.equal(servicePayload(42).text, undefined);
});

test("ошибка показывает понятную причину", () => {
  assert.equal(errorMessage(new Error("нет связи")), "нет связи");
  assert.equal(errorMessage({ error: { message: "HTTP 401" } }), "HTTP 401");
  assert.equal(errorMessage("сбой"), "сбой");
  assert.equal(errorMessage(null), "неизвестная ошибка");
  assert.equal(errorMessage({ code: "not_found" }), "ошибка: not_found");
});

test("профиль при повторном сохранении сохраняет идентификатор", () => {
  const draft = profileDraft({ name: "Новый" });
  assert.match(draft.id, /^profile_/);
  const again = profileDraft({ name: "Новый", id: "profile_other" }, draft);
  assert.equal(again.id, draft.id);
  assert.equal(again.name, "Новый");
  assert.equal(again.max_optional, 8);
  assert.equal(again.group_by_system, true);
  assert.equal(again.show_states, true);
  assert.equal(again.compact_grouping, true);
});

test("профиль переносит обязательные показатели и расписание", () => {
  const draft = profileDraft({
    name: "Вечерний",
    scheduleTimes: ["20:00"],
    weekdays: [0, 1],
    required: ["m1", "m2"],
    optional: ["m3"],
    maxOptional: 3,
    groupBySystem: false,
    compactGrouping: false,
    showStates: false,
  });
  assert.equal(draft.report_type, "scheduled");
  assert.deepEqual(draft.required_measurement_ids, ["m1", "m2"]);
  assert.deepEqual(draft.optional_measurement_ids, ["m3"]);
  assert.equal(draft.max_optional, 3);
  assert.equal(draft.group_by_system, false);
  assert.equal(draft.show_states, false);
  assert.equal(draft.compact_grouping, false);
});

test("расписание разбирается без пустых слотов", () => {
  assert.deepEqual(parseSchedule(" 08:00, , 20:00 "), ["08:00", "20:00"]);
  assert.deepEqual(parseSchedule(""), []);
});
