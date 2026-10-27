export type ConvertMode = "scan" | "native";

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
  const res = await fetch("/convert/analyze", { method: "POST", body: form });
  if (!res.ok) {
    const detail = await res.json().catch(() => ({}));
    throw new Error(detail.detail || `Анализ не удался (${res.status})`);
  }
  return (await res.json()) as AnalyzeResult;
}

export interface JobStatus {
  job_id: string;
  status: "queued" | "running" | "done" | "error";
  stage: string;
  progress: number;
  filename: string;
  error: string | null;
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
  const res = await fetch("/convert/profile");
  if (!res.ok) throw new Error(`Профиль недоступен (${res.status})`);
  return (await res.json()) as MachineProfile;
}

// Догоняет уже готовый DOCX LLM-доочисткой (п.3.2): распознавание не
// повторяется, работа идёт по сохранённым в файле жёлтым подсветкам.
export async function startPolish(jobId: string): Promise<void> {
  const res = await fetch(`/convert/polish/${jobId}`, { method: "POST" });
  if (!res.ok) {
    const detail = await res.json().catch(() => ({}));
    throw new Error(detail.detail || `Доочистка не запустилась (${res.status})`);
  }
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
  const res = await fetch(`/convert/${mode}`, { method: "POST", body: form });
  if (!res.ok) {
    const detail = await res.json().catch(() => ({}));
    throw new Error(detail.detail || `Ошибка запуска (${res.status})`);
  }
  const data = await res.json();
  return data.job_id as string;
}

export async function getStatus(jobId: string): Promise<JobStatus> {
  const res = await fetch(`/convert/status/${jobId}`);
  if (!res.ok) throw new Error(`Статус недоступен (${res.status})`);
  return (await res.json()) as JobStatus;
}

// Опрашивает статус раз в секунду, пока не done/error. onUpdate — для прогресс-бара.
export async function pollUntilDone(
  jobId: string,
  onUpdate: (s: JobStatus) => void,
): Promise<JobStatus> {
  for (;;) {
    const s = await getStatus(jobId);
    onUpdate(s);
    if (s.status === "done" || s.status === "error") return s;
    await new Promise((r) => setTimeout(r, 1000));
  }
}

// Скачивает готовый DOCX и инициирует сохранение в браузере.
export async function downloadResult(jobId: string, filename: string): Promise<void> {
  const res = await fetch(`/convert/download/${jobId}`);
  if (!res.ok) throw new Error(`Скачивание не удалось (${res.status})`);
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
