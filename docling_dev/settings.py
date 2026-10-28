"""
settings.py
───────────
Настройки, которые пользователь меняет из интерфейса: ресурсный профиль и
предлагать ли LLM-доочистку по умолчанию.

ЗАЧЕМ ОТДЕЛЬНО ОТ `profiles.py`. Профиль выбирается по железу
(`profiles.detect`) и это правильное поведение по умолчанию, но юрист на своей
машине должен иметь право сказать «мне нужно быстрее» или «не тронь мне
процессор», не трогая переменные окружения. Приложение sberAct рисует это в
своём диалоге настроек, поэтому выбор надо где-то хранить.

ПОРЯДОК СТАРШИНСТВА (от сильного к слабому):
  1. аргумент в коде (`build_converter(profile=...)`, поле `profile` в запросе);
  2. переменная окружения `CONVERTER_PROFILE` — ею гоняются замеры и A/B, и
     перебивать её сохранённым файлом нельзя: иначе снимки скорости начнут
     врать. Интерфейсу об этом сообщается (`locked_by_env`), чтобы он показал
     причину, а не молча игнорировал выбор пользователя;
  3. сохранённый выбор пользователя — этот файл;
  4. определение по машине.

ГДЕ ХРАНИМ. `%LOCALAPPDATA%/converter_ocr/settings.json`, а не рядом с
программой и не в TEMP: папка установки может быть только для чтения
(Program Files), а TEMP чистится системой — настройка пережить этого не должна.
Путь переопределяется `CONVERTER_SETTINGS_PATH`.
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
from pathlib import Path

log = logging.getLogger(__name__)

_LOCK = threading.Lock()
# Кэш прочитанного файла: настройки читаются на каждую конвертацию и на каждый
# запрос статуса профиля, а меняются раз в жизни.
_CACHE: dict | None = None

PROFILE_CHOICES = ("auto", "lean", "balanced", "full")


def path() -> Path:
    """Файл настроек. `CONVERTER_SETTINGS_PATH` перебивает."""
    override = os.environ.get("CONVERTER_SETTINGS_PATH", "").strip()
    if override:
        return Path(override)
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("XDG_CONFIG_HOME")
    if not base:
        # Крайний случай (нестандартный профиль Windows, Linux-контейнер):
        # лучше временная папка, чем отказ сохранять.
        base = tempfile.gettempdir()
    return Path(base) / "converter_ocr" / "settings.json"


def load() -> dict:
    """Сохранённые настройки. Пустой словарь — настроек нет.

    Нечитаемый файл не ошибка: настройка не критична, работаем на значениях по
    умолчанию и говорим об этом в лог.
    """
    global _CACHE
    with _LOCK:
        if _CACHE is not None:
            return dict(_CACHE)
    data: dict = {}
    p = path()
    try:
        if p.is_file():
            raw = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                data = raw
    except (OSError, ValueError) as exc:
        log.warning("settings: %s не читается (%s) — беру значения по умолчанию",
                    p, exc)
    with _LOCK:
        _CACHE = dict(data)
    return dict(data)


def save(**changes) -> dict:
    """Записать настройки (только переданные ключи) и вернуть все.

    `None` удаляет ключ — так интерфейс возвращает значение «как решит
    программа». Запись атомарная: оборванный JSON стоил бы настройки.
    """
    global _CACHE
    data = load()
    for key, value in changes.items():
        if value is None:
            data.pop(key, None)
        else:
            data[key] = value
    p = path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(p.name + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1),
                       encoding="utf-8")
        os.replace(tmp, p)
    except OSError as exc:
        # Настройка не сохранится — это не повод ломать работу: вернём то, что
        # будет действовать до перезапуска.
        log.warning("settings: не удалось записать %s (%s)", p, exc)
    with _LOCK:
        _CACHE = dict(data)
    return dict(data)


def reset_cache() -> None:
    """Забыть прочитанное (тесты и смена `CONVERTER_SETTINGS_PATH`)."""
    global _CACHE
    with _LOCK:
        _CACHE = None


def env_profile() -> str | None:
    """Профиль из окружения, если он задан и осмыслен."""
    raw = os.environ.get("CONVERTER_PROFILE", "").strip().lower()
    return raw if raw in PROFILE_CHOICES else None


def saved_profile() -> str | None:
    """Профиль, сохранённый пользователем."""
    raw = str(load().get("profile", "")).strip().lower()
    return raw if raw in PROFILE_CHOICES else None


def profile_name() -> str | None:
    """Имя профиля с учётом старшинства: окружение → сохранённый → None.

    None означает «решай по машине» — дальше работает `profiles.detect`.
    """
    return env_profile() or saved_profile()


def profile_source() -> str:
    """Откуда взялся профиль: env | user | auto. Нужно интерфейсу, чтобы
    объяснить, почему его выбор не применился."""
    if env_profile():
        return "env"
    if saved_profile():
        return "user"
    return "auto"


def llm_default(profile_value: bool) -> bool:
    """Предлагать ли LLM-доочистку. Выбор пользователя старше профиля машины:
    на слабой машине её не предлагают по умолчанию, но запрещать нельзя."""
    saved = load().get("llm_default")
    return bool(saved) if isinstance(saved, bool) else profile_value
