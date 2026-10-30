/* Чистые помощники панели: разбор ответов Home Assistant без «undefined».

   Вынесены отдельным модулем, чтобы их можно было проверять в Node без DOM.
*/

export function servicePayload(response) {
  if (response === null || response === undefined) {
    return {};
  }
  if (typeof response === "string") {
    return { text: response };
  }
  if (typeof response !== "object" || Array.isArray(response)) {
    return {};
  }
  if (typeof response.text === "string") {
    return response;
  }
  for (const key of ["response", "service_response", "serviceResponse", "result"]) {
    const value = response[key];
    if (value && typeof value === "object") {
      const payload = servicePayload(value);
      if (Object.keys(payload).length) {
        return payload;
      }
    }
  }
  return response;
}

export function reportMessage(response) {
  const payload = servicePayload(response);
  const text = typeof payload.text === "string" ? payload.text.trim() : "";
  if (!text) {
    return null;
  }
  return {
    text,
    usedAi: payload.used_ai === true || payload.usedAi === true,
    detail: typeof payload.detail === "string" ? payload.detail : "",
    attention: typeof payload.attention === "string" ? payload.attention : "",
  };
}

export function errorMessage(err) {
  if (!err) {
    return "неизвестная ошибка";
  }
  if (typeof err === "string") {
    return err.trim() || "неизвестная ошибка";
  }
  if (typeof err.message === "string" && err.message) {
    return err.message;
  }
  if (err.error && typeof err.error.message === "string" && err.error.message) {
    return err.error.message;
  }
  if (err.body && typeof err.body.message === "string" && err.body.message) {
    return err.body.message;
  }
  if (typeof err.code === "string" && err.code) {
    return `ошибка: ${err.code}`;
  }
  try {
    const text = JSON.stringify(err);
    return text && text !== "{}" ? text : "неизвестная ошибка";
  } catch {
    return "неизвестная ошибка";
  }
}

export function profileDraft(values, existing = null) {
  const base = existing || {};
  const scheduleTimes = Array.isArray(values.scheduleTimes) ? values.scheduleTimes : [];
  const weekdays = Array.isArray(values.weekdays) ? values.weekdays : [];
  return {
    id: base.id || values.id || `profile_${Math.random().toString(36).slice(2, 10)}`,
    name: (values.name || "").trim() || "Отчёт",
    locale: values.locale || "ru",
    detail: values.detail || "adaptive",
    timezone: values.timezone || "UTC",
    report_type: scheduleTimes.length ? "scheduled" : "on_demand",
    schedule_times: scheduleTimes,
    schedule_weekdays: weekdays,
    required_measurement_ids: Array.isArray(values.required) ? values.required : [],
    optional_measurement_ids: Array.isArray(values.optional) ? values.optional : [],
    measurement_order: Array.isArray(values.order) ? values.order : [],
    max_optional: Number.isFinite(values.maxOptional) ? values.maxOptional : 8,
    group_by_system: values.groupBySystem !== false,
    show_states: values.showStates !== false,
  };
}

export function readMultiSelect(root, selector) {
  const element = root.querySelector(selector);
  if (!element) {
    return [];
  }
  return Array.from(element.selectedOptions || []).map((option) => option.value);
}

export function parseSchedule(value) {
  return (value || "")
    .split(",")
    .map((item) => item.trim())
    .filter(Boolean);
}
