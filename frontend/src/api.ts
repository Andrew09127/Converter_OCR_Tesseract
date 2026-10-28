export type ConvertMode = "scan" | "native";

// Ошибка конвертации с машинным кодом (п.4.2). Сервер отдаёт detail объектом
// {code, message, hint}, и показать надо именно их, а не "[object Object]".
//
// `kind` (п.4.4) различает СЕТЕВЫЕ сбои, которые раньше все сливались в
// «Failed to fetch» — сообщение браузера, по которому пользователю нечего
// делать:
//   offline  — сервер не ответил вовсе (не запущен, упал, прокси молчит);
//   server   — ответил 5xx, то есть сломался внутри;
//   notfound — 404: задачи нет (сервер перезапускали и временные файлы ушли);
//   gone     — 410: результат уже удалён;
//   http     — прочий отказ с объяснением от сервера.
export type FailureKind = "offline" | "server" | "notfound" | "gone" | "http";

export interface ConvertFailure extends Error {
  code?: string;
  hint?: string;
  kind?: FailureKind;
  status?: number;
}

const OFFLINE_TEXT =
  "Сервер конвертера не отвечает. Проверьте, что программа запущена, " +
  "и попробуйте снова.";

function kindOf(status: number): FailureKind {
  if (status === 404) return "notfound";
  if (status === 410) return "gone";
  if (status >= 500) return "server";
  return "http";
}

async function failure(res: Response, fallback: string): Promise<ConvertFailure> {
  const body = await res.json().catch(() => ({} as any));
  const d = (body as any)?.detail;
  let err: ConvertFailure;
  if (d && typeof d === "object") {
    err = new Error([d.message, d.hint].filter(Boolean).join(" ")) as ConvertFailure;
    err.code = d.code;
    err.hint = d.hint;
  } else if (typeof d === "string" && d) {
    err = new Error(d) as ConvertFailure;
  } else if (res.status >= 500) {
    err = new Error(
      `${fallback}: сервер конвертера ответил ошибкой ${res.status}. ` +
        "Подробности — в логе программы.",
    ) as ConvertFailure;
  } else {
    err = new Error(`${fallback} (${res.status})`) as ConvertFailure;
  }
  err.kind = kindOf(res.status);
  err.status = res.status;
  return err;
}

// Любой запрос к конвертеру: сам fetch бросает TypeError, когда сервера нет, и
// браузер показывает «Failed to fetch». Здесь это превращается во внятный текст.
async function send(url: string, init?: RequestInit): Promise<Response> {
  try {
    return await fetch(url, init);
  } catch {
    const err = new Error(OFFLINE_TEXT) as ConvertFailure;
    err.kind = "offline";
    throw err;
  }
}

export interface ScanFlags {
  no_highlight: boolean;
  word_order: boolean;
  iim: boolean;
  ink_bold: boolean;
  ocr_preprocess: boolean;
}

export interface AnalyzeResult {
  suggested: ConvertMode;
  confidence: "high" | "low";
  reason: string;
}

// Быстро определяет тип PDF (скан/нативный), чтобы подсветить рекомендованный режим.
export async function analyzePdf(file: File): Promise<AnalyzeResult> {
  const form = new FormData();
  form.append("file", file);
  const res = await send("/convert/analyze", { method: "POST", body: form });
  if (!res.ok) throw await failure(res, "Анализ не удался");
  return (await res.json()) as AnalyzeResult;
}

export interface JobStatus {
  job_id: string;
  status: "queued" | "running" | "done" | "error";
  stage: string;
  progress: number;
  filename: string;
  error: string | null;
  // Пункт 4.2: машинный код причины и что делать. Текст `error` уже содержит
  // и то и другое одной строкой — код нужен интерфейсу для своих решений.
  error_code?: string | null;
  error_hint?: string | null;
  // Предупреждение до начала работы: текстовый слой в скан-режиме (п.1.5) или
  // нехватка памяти под этот файл (п.3.4). Не ошибка — режим не запрещается.
  warning?: string | null;
  mode?: ConvertMode;
  // Доочистка LLM уже прогонялась по этому документу (п.3.2).
  polished?: boolean;
  polish_fixed?: number | null;
}

// Ресурсный профиль машины (фаза 3). Нужен, чтобы НЕ предлагать LLM-доочистку
// по умолчанию на слабом ПК: модель 1.5B стоит ~1.6 ГБ пика и заметного времени.
export interface MachineProfile {
  profile: "lean" | "balanced" | "full";
  note: string;
  ocr_workers: number;
  num_threads: number;
  llm_default: boolean;
  cores: number;
  ram_gb: number;
  free_gb: number;
}

export async function getProfile(): Promise<MachineProfile> {
  const res = await send("/convert/profile");
  if (!res.ok) throw await failure(res, "Профиль недоступен");
  return (await res.json()) as MachineProfile;
}

// Догоняет уже готовый DOCX LLM-доочисткой (п.3.2): распознавание не
// повторяется, работа идёт по сохранённым в файле жёлтым подсветкам.
export async function startPolish(jobId: string): Promise<void> {
  const res = await send(`/convert/polish/${jobId}`, { method: "POST" });
  if (!res.ok) throw await failure(res, "Доочистка не запустилась");
}

// Запускает конвертацию. Для скана прикладывает флаги, для нативного — только файл.
export async function startConversion(
  mode: ConvertMode,
  file: File,
  flags?: ScanFlags,
): Promise<string> {
  const form = new FormData();
  form.append("file", file);
  if (mode === "scan" && flags) {
    form.append("no_highlight", String(flags.no_highlight));
    form.append("word_order", String(flags.word_order));
    form.append("iim", String(flags.iim));
    form.append("ink_bold", String(flags.ink_bold));
    form.append("ocr_preprocess", String(flags.ocr_preprocess));
  }
  const res = await send(`/convert/${mode}`, { method: "POST", body: form });
  if (!res.ok) throw await failure(res, "Ошибка запуска");
  const data = await res.json();
  return data.job_id as string;
}

export async function getStatus(jobId: string): Promise<JobStatus> {
  const res = await send(`/convert/status/${jobId}`);
  if (!res.ok) throw await failure(res, "Статус недоступен");
  return (await res.json()) as JobStatus;
}

// Готовность окружения (п.4.3): tesseract с языками, собранные модели, место на
// диске и в памяти. Отвечает 200 даже когда не готово — смотреть надо `ready`.
export interface HealthItem {
  name: string;
  ok: boolean;
  critical: boolean;
  detail: string;
}

export interface Health {
  ready: boolean;
  problems: string[];
  items: HealthItem[];
  warmup?: string;
}

export async function getHealth(): Promise<Health> {
  const res = await send("/convert/health");
  if (!res.ok) throw await failure(res, "Проверка окружения не удалась");
  return (await res.json()) as Health;
}

// Сколько подряд неудачных опросов терпим и с какой паузой (п.4.4). Конвертация
// идёт в фоне на сервере, поэтому единичный сетевой сбой — не повод объявлять
// задачу провалившейся: перезапуск прокси или протухший keep-alive лечатся
// повтором. 6 попыток с нарастающей паузой дают около 25 секунд.
const POLL_RETRIES = 6;
const POLL_PAUSE_MS = 1000;

export async function pollUntilDone(
  jobId: string,
  onUpdate: (s: JobStatus) => void,
  // Вызывается, когда связь потеряна и идут повторы; null — связь вернулась.
  onTrouble?: (text: string | null) => void,
): Promise<JobStatus> {
  let failures = 0;
  for (;;) {
    try {
      const s = await getStatus(jobId);
      if (failures > 0) {
        failures = 0;
        onTrouble?.(null);
      }
      onUpdate(s);
      if (s.status === "done" || s.status === "error") return s;
    } catch (e) {
      const err = e as ConvertFailure;
      // 404 повторять бессмысленно: задачи на сервере нет. Так бывает, если
      // сервер перезапустили и временные файлы задачи уже удалены.
      if (err.kind === "notfound") {
        const gone = new Error(
          "Задача не найдена на сервере: скорее всего, программу перезапускали " +
            "и временные файлы удалены. Запустите конвертацию заново.",
        ) as ConvertFailure;
        gone.kind = "notfound";
        throw gone;
      }
      failures += 1;
      if (failures > POLL_RETRIES) {
        const lost = new Error(
          err.kind === "offline"
            ? "Связь с сервером конвертера потеряна: он перестал отвечать. " +
              "Задача могла дойти до конца — перезапустите программу и " +
              "попробуйте снова."
            : `Связь с сервером конвертера потеряна: ${err.message}`,
        ) as ConvertFailure;
        lost.kind = err.kind ?? "offline";
        throw lost;
      }
      onTrouble?.(
        `Связь с сервером потеряна, повтор ${failures} из ${POLL_RETRIES}…`,
      );
      // Пауза растёт: сервер мог перезапускаться, мгновенные повторы не помогут.
      await new Promise((r) => setTimeout(r, POLL_PAUSE_MS * failures));
      continue;
    }
    await new Promise((r) => setTimeout(r, POLL_PAUSE_MS));
  }
}

// Скачивает готовый DOCX и инициирует сохранение в браузере.
export async function downloadResult(jobId: string, filename: string): Promise<void> {
  const res = await send(`/convert/download/${jobId}`);
  if (!res.ok) throw await failure(res, "Скачивание не удалось");
  const blob = await res.blob();
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
}
