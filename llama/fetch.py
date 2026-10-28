# -*- coding: utf-8 -*-
"""Доставка бинарников llama.cpp (llama-server) под текущую платформу.

Почему готовые бинарники, а не сборка из исходников: сборка потребовала бы на
машине разработчика CMake, компилятор C++ и Vulkan SDK, а под Astra Linux —
второй такой же набор. У проекта уже есть незакрытый долг по офлайн-сборке, и
удваивать его незачем. Официальные сборки llama.cpp содержат ровно то, что нам
нужно: Vulkan-бэкенд плюс варианты CPU под конкретные архитектуры (zen4,
alderlake, haswell...), между которыми llama.cpp выбирает сама.

Скачивается ОДИН РАЗ при подготовке дистрибутива. У пользователя интернета не
требуется никогда: llama-server слушает только 127.0.0.1 и наружу не ходит.

Запуск:  python converter/llama/fetch.py [win|linux|all]
"""
import io
import os
import platform
import sys
import tarfile
import urllib.request
import zipfile

BUILD = "b10509"
BASE = "https://github.com/ggml-org/llama.cpp/releases/download/" + BUILD + "/"
HERE = os.path.dirname(os.path.abspath(__file__))

# Vulkan-сборки: одна библиотека покрывает Intel, AMD и NVIDIA, в отличие от
# CUDA (только NVIDIA) и ROCm (только AMD). Там же лежат и CPU-варианты, так
# что машина без пригодного видеоядра ничего не теряет.
TARGETS = {
    "win": ("llama-" + BUILD + "-bin-win-vulkan-x64.zip", "win"),
    "linux": ("llama-" + BUILD + "-bin-ubuntu-vulkan-x64.tar.gz", "linux"),
}


def _download(url: str, dst: str) -> None:
    print("качаю", os.path.basename(url), flush=True)
    with urllib.request.urlopen(url, timeout=120) as r:
        data = r.read()
    with open(dst, "wb") as f:
        f.write(data)
    print("  %.1f МБ" % (len(data) / 1048576), flush=True)


def fetch(target: str) -> str:
    name, subdir = TARGETS[target]
    out_dir = os.path.join(HERE, subdir)
    marker = os.path.join(out_dir, ".build")
    if os.path.isfile(marker) and io.open(marker, encoding="utf-8").read().strip() == BUILD:
        print(target + ": уже " + BUILD + ", пропускаю", flush=True)
        return out_dir
    os.makedirs(out_dir, exist_ok=True)
    archive = os.path.join(HERE, name)
    if not os.path.isfile(archive):
        _download(BASE + name, archive)
    if name.endswith(".zip"):
        with zipfile.ZipFile(archive) as z:
            z.extractall(out_dir)
    else:
        with tarfile.open(archive) as t:
            t.extractall(out_dir)
    io.open(marker, "w", encoding="utf-8").write(BUILD)
    os.remove(archive)  # архив больше не нужен, а весит десятки мегабайт
    print(target + ": готово ->", out_dir, flush=True)
    return out_dir


if __name__ == "__main__":
    what = sys.argv[1] if len(sys.argv) > 1 else (
        "win" if platform.system() == "Windows" else "linux")
    for t in (list(TARGETS) if what == "all" else [what]):
        fetch(t)
