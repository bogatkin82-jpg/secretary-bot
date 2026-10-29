#!/usr/bin/env bash
# Установка бота-секретаря на сервер одной командой: bash setup.sh
set -e
cd "$(dirname "$0")"

SUDO=""
[ "$(id -u)" -ne 0 ] && SUDO="sudo"

if ! command -v docker >/dev/null 2>&1; then
  echo "== Устанавливаю Docker (1–2 минуты) =="
  curl -fsSL https://get.docker.com | $SUDO sh
fi
if ! $SUDO docker compose version >/dev/null 2>&1; then
  echo "== Доустанавливаю docker compose =="
  $SUDO apt-get update -qq
  $SUDO apt-get install -y docker-compose-plugin || $SUDO apt-get install -y docker-compose-v2
fi

echo
echo "== Введите значения (вставка: Cmd+V, затем Enter) =="
read -rp "Токен бота от BotFather: " BOT_TOKEN
read -rp "Ваш Telegram ID (только цифры): " OWNER_ID
echo
echo "Какую нейросеть подключить?  1 — GigaChat (Сбер),  2 — YandexGPT,  3 — Claude"
read -rp "Ваш выбор [1]: " CHOICE

{
  echo "BOT_TOKEN=${BOT_TOKEN// /}"
  echo "OWNER_ID=${OWNER_ID// /}"
  echo "TZ_NAME=Europe/Moscow"
} > .env

case "${CHOICE:-1}" in
  3)
    read -rp "Ключ Claude API (начинается с sk-ant-): " KEY
    echo "ANTHROPIC_API_KEY=${KEY// /}" >> .env
    curl -s -o /dev/null --max-time 10 https://api.anthropic.com || \
      echo "⚠️  api.anthropic.com недоступен с этого сервера — Claude работать не будет."
    ;;
  2)
    read -rp "Секретный ключ API Yandex AI Studio: " KEY
    read -rp "ID каталога (folder ID, начинается с b1g): " FOLDER
    echo "YANDEX_API_KEY=${KEY// /}" >> .env
    echo "YANDEX_FOLDER_ID=${FOLDER// /}" >> .env
    ;;
  *)
    read -rp "Ключ авторизации GigaChat (Authorization key, длинная строка): " KEY
    echo "GIGACHAT_AUTH_KEY=${KEY// /}" >> .env
    ;;
esac
chmod 600 .env

echo "== Собираю и запускаю бота =="
$SUDO docker compose up -d --build
sleep 8
$SUDO docker compose logs --tail 15
echo
echo "✅ Готово. Напишите своему боту /start в Telegram."
echo "   Логи: cd $(pwd) && docker compose logs -f"
