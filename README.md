# AI Cards Lead Intake

Сервис захвата лидов с выставки через:
- веб-форму (кнопки ВИЗИТКА и ГОЛОС),
- Telegram-бота (фото визитки и voice),
- автоматическое создание тикета в Zammad.

## Архитектура

- `web/` — мобильный интерфейс на 2 кнопки.
- `processor/` — FastAPI сервис OCR/STT + интеграции OpenAI/Zammad/Telegram.
- `caddy` — reverse proxy и TLS.
- `postgres` + `n8n` — сервисные компоненты (n8n доступен в `/n8n/`).

## Быстрый запуск

1. Скопируйте файл окружения:

```bash
cp .env.example .env
```

2. Заполните обязательные параметры в `.env`:

- `DOMAIN` — домен сервиса.
- `POSTGRES_PASSWORD`
- `N8N_BASIC_AUTH_PASSWORD`
- `N8N_ENCRYPTION_KEY`
- `OPENAI_API_KEY`
- `ZAMMAD_BASE_URL`
- `ZAMMAD_API_TOKEN`
- `ZAMMAD_GROUP` (обычно `Managers`)
- `ZAMMAD_FALLBACK_CUSTOMER_EMAIL`
- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID` (можно оставить пустым и определить позже)
- `TELEGRAM_WEBHOOK_SECRET` (длинная случайная строка)

3. Поднимите стек:

```bash
docker compose up -d --build
```

4. Установите webhook у Telegram-бота:

```bash
curl -s "https://api.telegram.org/bot<TELEGRAM_BOT_TOKEN>/setWebhook" \
	-d "url=https://<DOMAIN>/webhook/telegram" \
	-d "secret_token=<TELEGRAM_WEBHOOK_SECRET>" \
	-d "drop_pending_updates=true"
```

5. Проверьте статус webhook:

```bash
curl -s "https://api.telegram.org/bot<TELEGRAM_BOT_TOKEN>/getWebhookInfo"
```

## Эндпоинты

- `GET /healthz` — health check.
- `POST /webhook/card` — загрузка фото визитки через веб.
- `POST /webhook/voice` — загрузка аудио через веб.
- `POST /webhook/telegram` — входящий webhook Telegram.
- `GET /webhook/telegram-test` — тест исходящего сообщения в Telegram.
- `GET /n8n/` — UI n8n.

## Что создается в Zammad

- Тикет с полями лида: имя, компания, телефон, email, должность, источник, комментарий.
- Для голоса дополнительно прикладывается исходный аудиофайл к статье тикета.

## Прод-деплой на удаленный сервер

Пример для Linux-хоста:

```bash
mkdir -p /opt/ai-cards
cd /opt/ai-cards
git clone <repo_url> .
cp .env.example .env
# заполните .env
docker compose up -d --build
```

После запуска:
- Проверьте `https://<DOMAIN>/healthz`.
- Проверьте `https://<DOMAIN>/webhook/telegram-test`.
- Отправьте в бота `/start`, фото визитки и voice для e2e проверки.

## Безопасность

- Никогда не коммитьте `.env`.
- Регулярно ротируйте `OPENAI_API_KEY`, `ZAMMAD_API_TOKEN`, `TELEGRAM_BOT_TOKEN`.
- Держите `TELEGRAM_WEBHOOK_SECRET` уникальным и длинным.
- При утечке сразу:
	1. Перевыпустить токены,
	2. Обновить `.env`,
	3. Перезапустить `processor`.
