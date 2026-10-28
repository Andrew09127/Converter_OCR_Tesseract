"""
main.py
Локальный dev-сервер конвертера. Поднимает HTTP-API (docling_dev/api.py) на
127.0.0.1, чтобы разрабатывать и отлаживать React-фронт, не собирая Electron.

Запуск:
  ./.venv/Scripts/python.exe main.py            # http://127.0.0.1:8000

Полностью локально — слушает только loopback, в сеть ничего не уходит.

В приложении sberAct этот файл не нужен: их python-backend/main.py подключит
тот же роутер строкой `app.include_router(converter_router)`.
"""
from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
except (AttributeError, ValueError):
    pass

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s  %(levelname)s  %(message)s")
for _lib in ("docling", "PIL", "torch", "transformers", "easyocr"):
    logging.getLogger(_lib).setLevel(logging.WARNING)

_HERE = Path(__file__).parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from docling_dev.api import router as converter_router, engine_ready, start_warmup
# Теневые LLM-подсказки по полям. Живут здесь же, потому что здесь уже есть и
# llama-cpp, и файл модели, и жизненный цикл процесса (запуск по требованию,
# выгрузка по простою) — второй такой рантайм в бэкенде пришлось бы кормить
# теми же 2.3 ГБ и отдельно решать под Astra Linux.

app = FastAPI(title="PDF → Word конвертер (локальный)")

# Vite-фронт в деве крутится на 127.0.0.1:5173 — разрешаем ему ходить к API.
# Только loopback-origin'ы; наружу сервер всё равно не слушает.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://127.0.0.1:5173", "http://localhost:5173"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(converter_router)


# CONVERTER_WARMUP сводит два прежних смысла в один контракт:
#   не задана       — при старте не греем, но POST /convert/warmup работает;
#   1|eager|on|true — греем сразу при старте процесса;
#   0|off|no|false  — не греем вообще, маршрут отвечает «skipped».
_WARMUP_EAGER = ("1", "eager", "on", "true", "yes")


@app.on_event("startup")
async def _warmup() -> None:
    """Прогрев движка при старте — по требованию (CONVERTER_WARMUP=1).

    По умолчанию выключен: нативным PDF движок не нужен вовсе, а он тянет
    docling с torch на 15-40 с и гигабайты памяти. Кто знает, что документ
    сканированный, зовёт POST /convert/warmup — фронт делает это сразу после
    классификации, пока пользователь ещё смотрит на выбор режима.
    """
    if os.environ.get("CONVERTER_WARMUP", "").strip().lower() in _WARMUP_EAGER:
        start_warmup()


@app.get("/health")
async def health():
    return {"status": "ok", "converter_ready": engine_ready()}


# Если фронт уже собран (frontend/dist) — раздаём его статикой по корню,
# тогда всё приложение доступно на одном порту без отдельного Vite.
_DIST = _HERE / "frontend" / "dist"
if _DIST.is_dir():
    app.mount("/", StaticFiles(directory=str(_DIST), html=True), name="frontend")


if __name__ == "__main__":
    # host строго 127.0.0.1 — никакой выдачи наружу.
    uvicorn.run(app, host="127.0.0.1", port=8000, log_level="info")
