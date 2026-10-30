/* Проверка ответа сервиса тем же кодом, который использует панель. */
import { reportMessage } from "../../custom_components/ha_smart_analyst/frontend/panel-helpers.js";

const chunks = [];
for await (const chunk of process.stdin) {
  chunks.push(chunk);
}
const response = JSON.parse(Buffer.concat(chunks).toString("utf8"));
process.stdout.write(JSON.stringify(reportMessage(response)));
