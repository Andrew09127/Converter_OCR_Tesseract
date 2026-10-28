import { useEffect, useRef, useState } from "react";
import {
  Accordion,
  AccordionDetails,
  AccordionSummary,
  Alert,
  Box,
  Button,
  Checkbox,
  Chip,
  Divider,
  FormControlLabel,
  LinearProgress,
  MenuItem,
  TextField,
  Paper,
  Stack,
  Typography,
} from "@mui/material";
import ExpandMoreIcon from "@mui/icons-material/ExpandMore";
import PictureAsPdfIcon from "@mui/icons-material/PictureAsPdf";
import PsychologyIcon from "@mui/icons-material/Psychology";
import DescriptionIcon from "@mui/icons-material/Description";
import CloseIcon from "@mui/icons-material/Close";
import {
  ConvertMode,
  Health,
  MachineProfile,
  ScanFlags,
  ProfileChoice,
  analyzePdf,
  cancelJob,
  downloadResult,
  getJobLog,
  getSettings,
  saveSettings,
  getHealth,
  getProfile,
  pollUntilDone,
  startConversion,
  startPolish,
} from "./api";

// Названия ресурсных профилей по-русски (их выбирает бэкенд по машине).
const PROFILE_RU: Record<string, string> = {
  lean: "экономный",
  balanced: "сбалансированный",
  full: "полный",
};

// Версия приложения (показывается в шапке и памятке).
const VERSION = "2.0.2";

// Описание настроек распознавания — человеческими словами для рядового пользователя.
//   invert: галочка показывает обратное значение флага (для «обратных» флагов
//           вроде no_highlight), чтобы «галочка стоит = функция включена».
//   cli:    техническое имя флага — мелкой подписью, на случай обращения в поддержку.
const FLAG_DEFS: {
  key: keyof ScanFlags;
  cli: string;
  title: string;
  desc: string;
  invert?: boolean;
}[] = [
  {
    key: "no_highlight",
    cli: "--no-highlight",
    invert: true,
    title: "Подсвечивать сомнительные слова",
    desc: "Выделяет жёлтым слова, в распознавании которых программа не уверена — удобно, чтобы быстро найти и проверить их вручную. Включено по умолчанию.",
  },
  {
    key: "iim",
    cli: "--iim",
    title: "Улучшать текст нейросетью (ИИ)",
    desc: "Локальная нейросеть исправляет оставшиеся ошибки распознавания и делает текст чище. Работает чуть дольше. Включено по умолчанию.",
  },
  {
    key: "ocr_preprocess",
    cli: "--ocr-preprocess",
    title: "Улучшать качество плохих сканов",
    desc: "Выравнивает наклон и повышает контраст перед распознаванием. Помогает тёмным, бледным и перекошенным сканам, но может ухудшить и без того чёткие. Включайте, если результат плохой.",
  },
  {
    key: "word_order",
    cli: "--word-order",
    title: "Восстанавливать порядок строк на сложных сканах",
    desc: "Помогает, когда колонки или строки в документе распознаются вперемешку. Обычно не требуется — порядок и так определяется верно.",
  },
  {
    key: "ink_bold",
    cli: "--ink-bold",
    title: "Определять жирный шрифт по толщине букв",
    desc: "Экспериментальная функция. На сканах срабатывает ненадёжно и может ошибаться. По умолчанию выключено.",
  },
];

const DEFAULT_FLAGS: ScanFlags = {
  no_highlight: false,
  word_order: false,
  iim: true,
  ink_bold: false,
  ocr_preprocess: false,
};

export default function ScanConverter() {
  const [file, setFile] = useState<File | null>(null);
  const [flags, setFlags] = useState<ScanFlags>(DEFAULT_FLAGS);
  const [busy, setBusy] = useState(false);
  const [progress, setProgress] = useState(0);
  const [status, setStatus] = useState("Ожидание выбора PDF-файла…");
  const [statusKind, setStatusKind] = useState<"info" | "success" | "error">("info");
  const [dragOver, setDragOver] = useState(false);
  // Рекомендованный анализатором режим — для подсветки кнопки. null — ещё не определён.
  const [suggestedMode, setSuggestedMode] = useState<ConvertMode | null>(null);
  const [analyzing, setAnalyzing] = useState(false);
  // Профиль машины (фаза 3): на слабом ПК LLM-доочистку не предлагаем заранее.
  const [profile, setProfile] = useState<MachineProfile | null>(null);
  // Выбор пользователя: «Автоматически» или конкретный профиль. Автовыбор —
  // поведение по умолчанию, но на своей машине человек вправе его изменить.
  const [profileChoice, setProfileChoice] = useState<ProfileChoice>("auto");
  // Профиль задан переменной окружения — выбор не применится, объясняем это.
  const [profileLocked, setProfileLocked] = useState(false);
  // Предупреждение до начала работы: текстовый слой (п.1.5) или нехватка
  // памяти под этот файл (п.3.4). Не ошибка — конвертацию не запрещаем.
  const [warning, setWarning] = useState<string | null>(null);
  // Готовность окружения (п.4.4): если нет языков Tesseract или не собраны
  // модели, сказать об этом надо СРАЗУ, а не после загрузки файла.
  const [health, setHealth] = useState<Health | null>(null);
  // Связь с сервером потеряна, идут повторы опроса (п.4.4). Не ошибка: задача
  // на сервере, скорее всего, продолжает идти.
  const [trouble, setTrouble] = useState<string | null>(null);
  // Лог сорвавшейся задачи (п.4.7): показываем по кнопке «Подробности», это
  // диагностика для разработчика, а не сообщение пользователю.
  const [jobLog, setJobLog] = useState<string | null>(null);
  // Последняя сорвавшаяся задача — по ней можно запросить лог.
  const [failedJob, setFailedJob] = useState<string | null>(null);
  // Идущая задача: по ней доступна отмена (п.4.6).
  const [activeJob, setActiveJob] = useState<string | null>(null);
  // Готовая скан-задача: по ней доступна доочистка текста (п.3.2).
  const [doneJob, setDoneJob] = useState<{ id: string; filename: string } | null>(null);
  const inputRef = useRef<HTMLInputElement>(null);

  // Профиль спрашиваем один раз при открытии: от него зависит, включать ли
  // галочку LLM-доочистки по умолчанию (на lean/balanced она стоит ~1.6 ГБ).
  useEffect(() => {
    getProfile()
      .then((p) => {
        setProfile(p);
        setFlags((prev) => ({ ...prev, iim: p.llm_default }));
      })
      .catch(() => {
        // Профиль не критичен: оставляем значения по умолчанию.
      });
    getSettings()
      .then((s) => {
        setProfileChoice((s.override.profile as ProfileChoice) ?? "auto");
        setProfileLocked(s.locked_by_env);
      })
      .catch(() => {
        // Настройки не критичны: останется «Автоматически».
      });
    // Готовность окружения спрашиваем тем же разом: эндпоинт кэширует ответ,
    // а пользователь узнаёт о нехватке языков или моделей до работы, а не после.
    getHealth()
      .then((h) => setHealth(h))
      .catch(() => {
        // Сервер не ответил — это покажется при первом же действии.
      });
  }, []);

  // Сбрасывает выбранный файл и связанное состояние (рекомендацию, прогресс).
  function clearFile() {
    setFile(null);
    setSuggestedMode(null);
    setProgress(0);
    setWarning(null);
    setDoneJob(null);
    setStatus("Ожидание выбора PDF-файла…");
    setStatusKind("info");
  }

  function pickFile(f: File | null) {
    if (!f) return;
    if (!f.name.toLowerCase().endsWith(".pdf")) {
      setStatus("Можно загружать только PDF-файлы.");
      setStatusKind("error");
      return;
    }
    setFile(f);
    setProgress(0);
    setSuggestedMode(null);
    setWarning(null);
    setDoneJob(null);
    setFailedJob(null);
    setJobLog(null);
    setStatusKind("info");
    setStatus("Определяем тип документа…");
    setAnalyzing(true);
    // Лёгкий синхронный анализ на бэке (без OCR) — подсветит рекомендованный режим.
    analyzePdf(f)
      .then((res) => {
        setSuggestedMode(res.suggested);
        setStatus(res.reason);
      })
      .catch(() => {
        // Анализ не критичен: при сбое просто не подсвечиваем, режим выбирает пользователь.
        setStatus("PDF добавлен. Выберите режим конвертации.");
      })
      .finally(() => setAnalyzing(false));
  }

  async function convert(mode: ConvertMode) {
    if (!file || busy) return;
    setBusy(true);
    setProgress(0);
    setStatusKind("info");
    setStatus("Загрузка файла…");
    try {
      const jobId = await startConversion(mode, file, mode === "scan" ? flags : undefined);
      setActiveJob(jobId);
      const final = await pollUntilDone(
        jobId,
        (s) => {
          setProgress(Math.round(s.progress * 100));
          setStatus(`Обработка: ${s.stage}`);
          if (s.warning) setWarning(s.warning);
        },
        setTrouble,
      );
      if (final.status === "done") {
        await downloadResult(jobId, final.filename);
        setProgress(100);
        setStatusKind("success");
        setStatus(`Готово: ${final.filename} скачан.`);
        // Доочистка имеет смысл только для скана и только если её не делали.
        if (mode === "scan" && !flags.iim && !final.polished) {
          setDoneJob({ id: jobId, filename: final.filename });
        }
      } else if (final.error_code === "CANCELLED") {
        // Отмену просил сам пользователь — это не ошибка.
        setProgress(0);
        setStatusKind("info");
        setStatus("Конвертация отменена. Файл не изменён.");
      } else {
        setStatusKind("error");
        setStatus(`Ошибка: ${final.error || "конвертация не удалась"}`);
        setFailedJob(jobId);
      }
    } catch (e) {
      setStatusKind("error");
      setStatus(`Ошибка: ${(e as Error).message}`);
    } finally {
      setBusy(false);
      setTrouble(null);
      setActiveJob(null);
    }
  }

  // Смена профиля: сохраняем на сервере, чтобы выбор пережил перезапуск, и
  // сразу перечитываем профиль — от него зависит дефолт доочистки.
  async function changeProfile(choice: ProfileChoice) {
    setProfileChoice(choice);
    try {
      const saved = await saveSettings({ profile: choice });
      setProfileLocked(saved.locked_by_env);
      const p = await getProfile();
      setProfile(p);
      setFlags((prev) => ({ ...prev, iim: p.llm_default }));
    } catch (e) {
      setStatusKind("error");
      setStatus(`Настройку не удалось сохранить: ${(e as Error).message}`);
    }
  }

  // Лог сорвавшейся задачи (п.4.7). Запрашиваем по кнопке: он может быть
  // большим, а нужен редко.
  async function showLog() {
    if (!failedJob) return;
    try {
      setJobLog(await getJobLog(failedJob));
    } catch (e) {
      setJobLog(`Лог недоступен: ${(e as Error).message}`);
    }
  }

  // Отмена идущей задачи (п.4.6). Итог придёт обычным опросом статуса,
  // поэтому здесь только просим и сообщаем, что просьба принята.
  async function cancel() {
    if (!activeJob) return;
    try {
      const state = await cancelJob(activeJob);
      setStatus(state === "cancelled"
        ? "Задача снята из очереди."
        : "Отменяем: конвертация остановится на ближайшей странице…");
    } catch (e) {
      setStatusKind("error");
      setStatus(`Отменить не удалось: ${(e as Error).message}`);
    }
  }

  // Доочистка готового документа (п.3.2): распознавание не повторяется, модель
  // проходит только по жёлтым подсветкам, сохранённым в самом DOCX.
  async function polish() {
    if (!doneJob || busy) return;
    setBusy(true);
    setProgress(0);
    setStatusKind("info");
    setStatus("Запускаем доочистку текста…");
    try {
      await startPolish(doneJob.id);
      const final = await pollUntilDone(
        doneJob.id,
        (s) => {
          setProgress(Math.round(s.progress * 100));
          setStatus(`Обработка: ${s.stage}`);
        },
        setTrouble,
      );
      await downloadResult(doneJob.id, final.filename);
      setProgress(100);
      setStatusKind(final.error ? "error" : "success");
      setStatus(
        final.error
          ? `Доочистка не удалась: ${final.error}. Исходный документ скачан.`
          : `Готово: ${final.filename} скачан (исправлено ${final.polish_fixed ?? 0}).`,
      );
      setDoneJob(null);
    } catch (e) {
      setStatusKind("error");
      setStatus(`Ошибка: ${(e as Error).message}`);
    } finally {
      setBusy(false);
      setTrouble(null);
    }
  }

  return (
    <Box sx={{ minHeight: "100vh", display: "flex", justifyContent: "center", py: 4, px: 2 }}>
      <Paper elevation={4} sx={{ width: 720, borderRadius: 4, overflow: "hidden" }}>
        {/* Шапка */}
        <Box sx={{ px: 3, py: 2, borderBottom: "1px solid #E5E7EB", display: "flex",
                   alignItems: "center", gap: 1.5 }}>
          <Box component="img" src="/logo.png" alt="logo"
               sx={{ width: 34, height: 34, objectFit: "contain" }} />
          <Typography variant="h6" fontWeight={700}>
            PDF → Word конвертер
          </Typography>
          <Chip label={`v${VERSION}`} size="small" color="primary" variant="outlined"
                sx={{ ml: 0.5, fontWeight: 600 }} />
          <Chip label="beta" size="small"
                sx={{ bgcolor: "#FEF3C7", color: "#92400E", fontWeight: 600 }} />
        </Box>

        <Stack spacing={2.5} sx={{ p: 3 }}>
          {/* Дропзона */}
          <Box
            onClick={() => inputRef.current?.click()}
            onDragOver={(e) => { e.preventDefault(); setDragOver(true); }}
            onDragLeave={() => setDragOver(false)}
            onDrop={(e) => {
              e.preventDefault();
              setDragOver(false);
              pickFile(e.dataTransfer.files?.[0] ?? null);
            }}
            sx={{
              border: "2px dashed",
              borderColor: dragOver ? "primary.main" : "#CBD5E1",
              bgcolor: dragOver ? "#E7F6EA" : "#fff",
              borderRadius: 3,
              py: 4,
              textAlign: "center",
              cursor: "pointer",
              transition: "all .15s",
            }}
          >
            <input
              ref={inputRef}
              type="file"
              accept=".pdf"
              hidden
              onChange={(e) => pickFile(e.target.files?.[0] ?? null)}
            />
            <PictureAsPdfIcon color="primary" sx={{ fontSize: 44 }} />
            <Typography variant="h6" fontWeight={700} mt={1}>
              Нажмите или перетащите PDF
            </Typography>
            <Typography variant="body2" color="text.secondary">
              два режима: распознавание сканов (LLM) или нативный PDF
            </Typography>
            {file && (
              <Box mt={1.5}>
                <Chip
                  color="primary"
                  variant="outlined"
                  label={file.name}
                  onDelete={busy ? undefined : clearFile}
                  deleteIcon={<CloseIcon />}
                />
              </Box>
            )}
          </Box>

          <Alert severity={statusKind} variant="outlined">{status}</Alert>

          {/* Лог сорвавшейся задачи (п.4.7): кнопка и, после нажатия, хвост
              лога. Нужен разработчику — на машине пользователя общий лог
              сервера недоступен. */}
          {failedJob && !busy && (
            <Box>
              {jobLog === null ? (
                <Button size="small" color="inherit" onClick={showLog}>
                  Подробности
                </Button>
              ) : (
                <Box
                  component="pre"
                  sx={{ m: 0, p: 1.5, maxHeight: 220, overflow: "auto",
                        bgcolor: "#F3F4F6", borderRadius: 2, fontSize: 12,
                        whiteSpace: "pre-wrap", wordBreak: "break-word" }}
                >
                  {jobLog}
                </Box>
              )}
            </Box>
          )}

          {/* Связь потеряна, но задача на сервере идёт: повторы опроса (п.4.4).
              Это не ошибка конвертации, поэтому своя плашка, а не красная. */}
          {trouble && (
            <Alert severity="warning" variant="outlined">{trouble}</Alert>
          )}

          {/* Окружение не готово (п.4.4): нет языков Tesseract, не собраны
              модели, кончается память или диск. Сказать это надо до работы. */}
          {health && !health.ready && (
            <Alert severity="error" variant="outlined">
              Окружение не готово: {health.problems.join(", ")}.
              {health.items
                .filter((i) => i.critical && !i.ok)
                .map((i) => ` ${i.name}: ${i.detail}`)
                .join("")}
            </Alert>
          )}

          {/* Предупреждение (текстовый слой в скан-режиме или нехватка памяти):
              не ошибка, конвертация идёт — поэтому отдельной плашкой. */}
          {warning && (
            <Alert severity="warning" variant="outlined" onClose={() => setWarning(null)}>
              {warning}
            </Alert>
          )}

          {/* Две карточки режимов с пояснениями.
              Анализатор подсвечивает рекомендованную карточку зелёной рамкой и делает
              её кнопку «success»-зелёной; обе кнопки остаются активны — выбор за пользователем. */}
          <Stack direction={{ xs: "column", sm: "row" }} spacing={2} alignItems="stretch">
            {/* Скан-режим */}
            {(() => {
              const recommended = suggestedMode === "scan";
              return (
            <Box sx={{ flex: 1, p: 2, borderRadius: 2, display: "flex", flexDirection: "column",
                       border: "2px solid", borderColor: recommended ? "success.main" : "#E5E7EB",
                       bgcolor: recommended ? "#F0FBF2" : "#fff", transition: "all .2s" }}>
              <Box sx={{ display: "flex", alignItems: "center", gap: 1, mb: 0.5 }}>
                <PsychologyIcon color="primary" fontSize="small" />
                <Typography variant="subtitle2" fontWeight={700}>
                  Отсканированный PDF
                </Typography>
                {recommended && (
                  <Chip label="рекомендуем" size="small" color="success"
                        sx={{ ml: "auto", fontWeight: 600, height: 20 }} />
                )}
              </Box>
              <Typography variant="caption" color="text.secondary" sx={{ mb: 1.5, flexGrow: 1 }}>
                Для документов-изображений (фото и сканы), где текст нельзя выделить.
                Распознаёт текст и восстанавливает структуру с помощью ИИ. Точнее, но
                заметно медленнее — учитывает настройки выше.
              </Typography>
              <Button
                fullWidth
                size="large"
                variant={recommended ? "contained" : "outlined"}
                color={recommended ? "success" : "primary"}
                startIcon={<PsychologyIcon />}
                disabled={!file || busy || analyzing}
                onClick={() => convert("scan")}
              >
                Конвертировать скан
              </Button>
            </Box>
              );
            })()}

            {/* Нативный режим */}
            {(() => {
              const recommended = suggestedMode === "native";
              return (
            <Box sx={{ flex: 1, p: 2, borderRadius: 2, display: "flex", flexDirection: "column",
                       border: "2px solid", borderColor: recommended ? "success.main" : "#E5E7EB",
                       bgcolor: recommended ? "#F0FBF2" : "#fff", transition: "all .2s" }}>
              <Box sx={{ display: "flex", alignItems: "center", gap: 1, mb: 0.5 }}>
                <DescriptionIcon color="primary" fontSize="small" />
                <Typography variant="subtitle2" fontWeight={700}>
                  Неотсканированный PDF
                </Typography>
                {recommended && (
                  <Chip label="рекомендуем" size="small" color="success"
                        sx={{ ml: "auto", fontWeight: 600, height: 20 }} />
                )}
              </Box>
              <Typography variant="caption" color="text.secondary" sx={{ mb: 1.5, flexGrow: 1 }}>
                Для документов с текстовым слоем (текст выделяется и копируется).
                Переносит текст, таблицы и картинки напрямую — быстро и точно.
                Распознавание и настройки выше не используются.
              </Typography>
              <Button
                fullWidth
                size="large"
                variant={recommended ? "contained" : "outlined"}
                color={recommended ? "success" : "primary"}
                startIcon={<DescriptionIcon />}
                disabled={!file || busy || analyzing}
                onClick={() => convert("native")}
              >
                Конвертировать PDF
              </Button>
            </Box>
              );
            })()}
          </Stack>

          {/* Прогресс — показывается только во время конвертации и после её завершения */}
          {(busy || progress > 0) && (
            <Box>
              <Box sx={{ display: "flex", justifyContent: "space-between", mb: 0.5 }}>
                <Typography variant="body2" color="text.secondary">Прогресс</Typography>
                <Typography variant="body2" color="text.secondary">{progress}%</Typography>
              </Box>
              <LinearProgress
                variant={busy && progress === 0 ? "indeterminate" : "determinate"}
                value={progress}
                color={statusKind === "success" ? "success" : "primary"}
                sx={{ height: 10, borderRadius: 5 }}
              />
              {/* Отмена (п.4.6): скан останавливается на ближайшей проверке,
                  задача из очереди снимается сразу. Нативный режим прервать
                  нельзя — сервер ответит отказом с объяснением. */}
              {busy && activeJob && (
                <Box sx={{ display: "flex", justifyContent: "flex-end", mt: 1 }}>
                  <Button size="small" color="inherit" onClick={cancel}>
                    Отменить
                  </Button>
                </Box>
              )}
            </Box>
          )}

          {/* Доочистка текста (п.3.2): предлагаем ПОСЛЕ получения документа, чтобы
              пользователь сам решил, платить ли за неё временем и памятью —
              модель 1.5B занимает около 1.6 ГБ. */}
          {doneJob && !busy && (
            <Alert
              severity="info"
              variant="outlined"
              action={
                <Button color="inherit" size="small" startIcon={<PsychologyIcon />}
                        onClick={polish}>
                  Доочистить
                </Button>
              }
            >
              Документ готов. Жёлтым выделены слова, в которых распознавание
              сомневается. Можно догнать их локальной моделью — это займёт ещё
              немного времени и памяти.
            </Alert>
          )}

          {/* Флаги (применяются к скан-режиму) */}
          <Accordion disableGutters sx={{ borderRadius: 2, "&:before": { display: "none" } }}
                     variant="outlined">
            <AccordionSummary expandIcon={<ExpandMoreIcon />}>
              <Typography fontWeight={700}>
                Дополнительные настройки распознавания
              </Typography>
            </AccordionSummary>
            <AccordionDetails>
              <Typography variant="body2" color="text.secondary" sx={{ mb: 1.5 }}>
                Эти настройки влияют только на режим «Отсканированный PDF». Для большинства
                документов менять их не нужно — значения по умолчанию подобраны оптимально.
              </Typography>
              {/* Профиль объясняет, почему часть настроек выключена: на слабой
                  машине доочистка не включается сама, она стоит ~1.6 ГБ. */}
              {profile && (
                <Typography variant="caption" color="text.secondary"
                            sx={{ display: "block", mb: 1.5 }}>
                  Этот компьютер: {profile.cores} ядер, {profile.ram_gb} ГБ памяти —
                  выбран режим «{PROFILE_RU[profile.profile]}».
                  {!profile.llm_default &&
                    " Доочистка текста по умолчанию выключена: её можно включить" +
                    " здесь или запустить кнопкой уже после конвертации."}
                </Typography>
              )}
              {/* Выбор профиля: по умолчанию решает машина, но человек на своей
                  машине вправе сказать «мне нужно быстрее» или «не грузи
                  процессор». Выбор хранится на сервере и переживает
                  перезапуск. */}
              <TextField
                select
                size="small"
                label="Режим использования ресурсов"
                value={profileChoice}
                onChange={(e) => changeProfile(e.target.value as ProfileChoice)}
                disabled={busy || profileLocked}
                helperText={
                  profileLocked
                    ? "Задано переменной окружения CONVERTER_PROFILE — выбор здесь не применится."
                    : "Экономный медленнее на ~20%, но занимает примерно на 500 МБ меньше памяти."
                }
                sx={{ mb: 2, minWidth: 320 }}
              >
                <MenuItem value="auto">Автоматически по компьютеру</MenuItem>
                <MenuItem value="lean">Экономный</MenuItem>
                <MenuItem value="balanced">Сбалансированный</MenuItem>
                <MenuItem value="full">Полный</MenuItem>
              </TextField>
              <Stack spacing={1}>
                {FLAG_DEFS.map((f) => {
                  // Для «обратных» флагов галочка показывает противоположное значение.
                  const checked = f.invert ? !flags[f.key] : flags[f.key];
                  return (
                    <FormControlLabel
                      key={f.key}
                      sx={{ alignItems: "flex-start", m: 0 }}
                      control={
                        <Checkbox
                          checked={checked}
                          disabled={busy}
                          onChange={(e) => {
                            const v = f.invert ? !e.target.checked : e.target.checked;
                            setFlags((prev) => ({ ...prev, [f.key]: v }));
                          }}
                        />
                      }
                      label={
                        <Box sx={{ py: 0.5 }}>
                          <Typography variant="body2" fontWeight={700}>
                            {f.title}
                          </Typography>
                          <Typography variant="caption" color="text.secondary"
                                      component="p" sx={{ mt: 0.25 }}>
                            {f.desc}
                          </Typography>
                          <Typography variant="caption" sx={{ color: "#9CA3AF" }}>
                            {f.cli}
                          </Typography>
                        </Box>
                      }
                    />
                  );
                })}
              </Stack>
            </AccordionDetails>
          </Accordion>

          {/* Памятка */}
          <Accordion disableGutters sx={{ borderRadius: 2, "&:before": { display: "none" } }}
                     variant="outlined">
            <AccordionSummary expandIcon={<ExpandMoreIcon />}>
              <Typography fontWeight={700}>Памятка</Typography>
            </AccordionSummary>
            <AccordionDetails>
              <Typography variant="body2" fontWeight={700} gutterBottom>
                Версия {VERSION} — первый публичный релиз (beta).
              </Typography>
              <Typography variant="body2" color="text.secondary" gutterBottom>
                Это первая версия конвертера, и она будет активно дорабатываться.
                Возможны неточности в распознавании текста и оформлении — особенно на
                сложных сканах. Готовый документ рекомендуем перед использованием
                просматривать и при необходимости править вручную.
              </Typography>
              <Typography variant="body2" color="text.secondary" gutterBottom>
                Все данные обрабатываются <b>полностью на вашем компьютере</b> — файлы
                никуда не загружаются и не передаются в интернет.
              </Typography>

              <Divider sx={{ my: 1.5 }} />

              <Typography variant="body2" fontWeight={700} gutterBottom>
                Как выбрать режим
              </Typography>
              <Typography variant="body2" color="text.secondary" gutterBottom>
                • <b>Отсканированный PDF</b> — если страницы являются изображениями
                (фото или скан документа) и текст в них нельзя выделить мышью.
              </Typography>
              <Typography variant="body2" color="text.secondary" gutterBottom>
                • <b>Неотсканированный PDF</b> — если в документе есть текстовый слой
                (текст выделяется и копируется). Работает быстро и точно.
              </Typography>
              <Typography variant="caption" color="text.secondary">
                Не уверены? Загрузите файл — конвертер сам определит тип документа и
                подсветит рекомендуемый режим зелёным.
              </Typography>

              <Divider sx={{ my: 1.5 }} />

              <Typography variant="body2" fontWeight={700} gutterBottom>
                Советы для лучшего результата
              </Typography>
              <Typography variant="body2" color="text.secondary">
                Загружайте сканы хорошего качества: без сильного размытия, обрезанных
                страниц, перекосов и слишком тёмного фона. Если распознавание плохое —
                включите «Улучшать качество плохих сканов» в дополнительных настройках.
              </Typography>
            </AccordionDetails>
          </Accordion>
        </Stack>
      </Paper>

      {/* Незаметная «подпись автора» — полупрозрачный логотип в правом нижнем углу.
          Не перехватывает клики, чуть проявляется при наведении. */}
      <Box
        component="img"
        src="/logo.png"
        alt=""
        title="PDF → Word конвертер"
        sx={{
          position: "fixed",
          right: 16,
          bottom: 16,
          width: 28,
          height: 28,
          objectFit: "contain",
          opacity: 0.25,
          pointerEvents: "none",
          userSelect: "none",
          transition: "opacity .2s",
          "&:hover": { opacity: 0.6 },
        }}
      />
    </Box>
  );
}
