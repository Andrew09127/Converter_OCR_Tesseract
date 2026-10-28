// -*- coding: utf-8 -*-
//
// tests/frontend_api.mjs
// ─────────────────────────────────────────────────────────────────────────────
// Проверка сетевой логики фронта (пункт 4.4): различение сбоев и повторы
// опроса. Запускается через `python tests/frontend_api.py` — он компилирует
// `frontend/src/api.ts` и передаёт сюда путь к собранному модулю.
//
// Тестового рантайма (vitest/jest) в проекте нет и добавлять его ради одного
// файла не стали: зависимость попала бы в офлайн-комплект. Здесь хватает
// подменённых `fetch` и `setTimeout`.
//
// `fetch` подменяется целиком — сервер не нужен. `setTimeout` тоже: иначе
// проверка повторов ждала бы 21 секунду по-настоящему, а нам нужны только
// длительности паузы.

const api = await import(process.argv[2]);

let calls = [];
let pauses = [];
let failed = 0;

// Паузы между повторами не ждём, но записываем: по ним видно, что задержка
// растёт, а не молотит сервер в цикле.
const realTimeout = globalThis.setTimeout;
globalThis.setTimeout = (fn, ms) => {
  pauses.push(ms);
  return realTimeout(fn, 0);
};

function mock(handler) {
  calls = [];
  pauses = [];
  globalThis.fetch = async (url, init) => {
    calls.push(String(url));
    return handler(String(url), init, calls.length);
  };
}

const json = (status, body) => ({
  ok: status >= 200 && status < 300,
  status,
  json: async () => body,
});

function check(name, cond, extra = "") {
  console.log(`  [${cond ? "OK  " : "СБОЙ"}] ${name}${extra ? " — " + extra : ""}`);
  if (!cond) failed += 1;
}

// ── сервер не отвечает вовсе: ровно тот случай, что давал «Failed to fetch» ──
mock(() => {
  throw new TypeError("Failed to fetch");
});
try {
  await api.getStatus("x");
  check("нет сервера → ошибка", false);
} catch (e) {
  check("нет сервера → kind=offline", e.kind === "offline", e.message);
  check("нет сервера → текст без «Failed to fetch»",
        !e.message.includes("Failed to fetch"));
}

// ── 5xx: сервер жив, но сломался внутри ─────────────────────────────────────
mock(() => json(500, {}));
try {
  await api.getStatus("x");
  check("500 → ошибка", false);
} catch (e) {
  check("500 → kind=server", e.kind === "server", e.message);
  check("500 → в тексте код ответа", e.message.includes("500"));
}

// ── структурный detail от сервера (п.4.2) ───────────────────────────────────
mock(() => json(400, {
  detail: {
    code: "PDF_ENCRYPTED",
    message: "PDF защищён паролем.",
    hint: "Снимите пароль.",
  },
}));
try {
  await api.startConversion("scan", new File([], "f.pdf"), undefined);
  check("400 → ошибка", false);
} catch (e) {
  check("400 → код проброшен", e.code === "PDF_ENCRYPTED", e.message);
  check("400 → текст = сообщение + подсказка",
        e.message === "PDF защищён паролем. Снимите пароль.", e.message);
}

// ── временный сбой при опросе: задача на сервере идёт, сдаваться рано ───────
const trouble = [];
mock((url, init, n) => {
  if (n <= 2) throw new TypeError("Failed to fetch");
  return json(200, { status: "done", stage: "Готово", progress: 1,
                     filename: "a.docx" });
});
let final = await api.pollUntilDone("x", () => {}, (t) => trouble.push(t));
check("временный сбой переживается", final.status === "done",
      `${calls.length} запроса`);
check("о повторах сообщено и о возврате связи тоже",
      trouble.length === 3 && trouble[2] === null, JSON.stringify(trouble));
check("пауза между повторами растёт",
      pauses[0] === 1000 && pauses[1] === 2000, JSON.stringify(pauses));

// ── 404 при опросе: повторять нечего, задачи на сервере нет ─────────────────
mock(() => json(404, { detail: "Задача не найдена." }));
try {
  await api.pollUntilDone("x", () => {}, () => {});
  check("404 → ошибка", false);
} catch (e) {
  check("404 → без повторов", calls.length === 1, `${calls.length} запрос(ов)`);
  check("404 → kind=notfound", e.kind === "notfound");
  check("404 → текст объясняет причину",
        e.message.includes("перезапуск"), e.message);
}

// ── связь не вернулась: сдаёмся, но внятно ──────────────────────────────────
mock(() => {
  throw new TypeError("Failed to fetch");
});
try {
  await api.pollUntilDone("x", () => {}, () => {});
  check("потеря связи → ошибка", false);
} catch (e) {
  check("потеря связи → 7 попыток (1 + 6 повторов)", calls.length === 7,
        `${calls.length}`);
  check("потеря связи → текст говорит, что делать",
        e.message.includes("перезапустите программу"), e.message);
}

// ── отмена задачи (п.4.6) ───────────────────────────────────────────────────
mock(() => json(200, { job_id: "x", state: "cancelling" }));
check("отмена идущей задачи", (await api.cancelJob("x")) === "cancelling");
mock(() => json(200, { job_id: "x", state: "cancelled" }));
check("снятие из очереди", (await api.cancelJob("x")) === "cancelled");
mock(() => json(409, { detail: "Эту работу нельзя прервать на ходу: нативный режим идёт внутри pdf2docx. Дождитесь завершения." }));
try {
  await api.cancelJob("x");
  check("409 на отмене → ошибка", false);
} catch (e) {
  check("409 на отмене → текст сервера, а не наш",
        e.message.includes("pdf2docx") && e.kind === "http", e.message);
}

// ── лог задачи (п.4.7) ──────────────────────────────────────────────────────
mock(() => json(200, { job_id: "x", text: "строка лога" }));
check("лог задачи читается", (await api.getJobLog("x")) === "строка лога");
check("лог просит хвост, а не весь файл",
      calls[0].includes("tail="), calls[0]);
mock(() => json(404, { detail: "Лог этой задачи недоступен." }));
try {
  await api.getJobLog("x");
  check("нет лога → ошибка", false);
} catch (e) {
  check("нет лога → kind=notfound и текст сервера",
        e.kind === "notfound" && e.message.includes("недоступен"), e.message);
}

// ── готовность окружения (п.4.3) ────────────────────────────────────────────
mock(() => json(200, { ready: false, problems: ["tesseract"], items: [] }));
const h = await api.getHealth();
check("health читается", h.ready === false && h.problems[0] === "tesseract");

console.log(failed ? `\nпровалено проверок: ${failed}` : "\nвсё в порядке");
process.exit(failed ? 1 : 0);
