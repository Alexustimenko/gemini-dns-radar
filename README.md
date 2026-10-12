# Gemini DNS Radar

Автоматический TOP-10 Android Private DNS (DoT), которые показывают технические признаки SmartDNS-маршрутизации для Gemini.

## Что делает проект

- Cloudflare Python Worker бесплатно отдаёт сайт и API.
- Workers KV хранит текущее состояние и историю.
- Python crawler запускается каждый час в GitHub Actions.
- Crawler автоматически получает DoT-кандидатов из публичных источников.
- Специализированные страницы про Gemini используются как повышающий доверие источник кандидатов, но DNS не попадает в TOP только из-за упоминания на странице.
- Каждый кандидат проверяется настоящим DNS-over-TLS на порту 853.
- Для `gemini.google.com`, `aistudio.google.com`, `notebooklm.google.com`, `generativelanguage.googleapis.com` анализируются A-записи.
- IP сравниваются с официально опубликованными диапазонами Google (`https://www.gstatic.com/ipranges/goog.json`). Если несколько Gemini-целей уходят на адреса вне сетей Google, это сильный автоматический сигнал SmartDNS-прокси.
- TOP-10 пересчитывается по текущему статусу, uptime 24h/7d, серии успешных проверок, задержке и источникам.

> Серверная проверка не может доказать на 100%, что конкретный Pixel у конкретного белорусского оператора откроет Gemini. Для абсолютной проверки нужен probe внутри белорусской сети. Этот проект намеренно показывает технический SmartDNS-сигнал и стабильность, а не выдуманную гарантию.

## Структура

```text
gemini-dns-radar/
├── src/entry.py
├── public/
│   ├── index.html
│   ├── app.js
│   └── styles.css
├── crawler/crawler.py
├── .github/workflows/refresh.yml
├── wrangler.jsonc
├── pyproject.toml
└── .gitignore
```

## 1. Установить инструменты

Cloudflare сейчас рекомендует `pywrangler` для Python Workers.

Нужны:

- Node.js
- `uv`
- аккаунт Cloudflare
- GitHub-репозиторий проекта

Проверка:

```bash
node -v
uv --version
```

## 2. Войти в Cloudflare

```bash
npx wrangler login
```

## 3. Развернуть Worker

Из корня проекта:

```bash
uvx --from workers-py pywrangler deploy
```

В `wrangler.jsonc` KV binding указан без ID. Современный Wrangler умеет автоматически создать недостающий KV resource при deploy.

После deploy получишь адрес вроде:

```text
https://gemini-dns-radar.<твой-subdomain>.workers.dev
```

## 4. Создать секрет Worker

Сгенерируй длинный случайный токен, например:

```bash
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

Затем:

```bash
npx wrangler secret put PUBLISH_TOKEN
```

Вставь токен и сохрани его — тот же токен понадобится GitHub Actions.

## 5. Залить проект в GitHub

```bash
git init
git add .
git commit -m "Initial Gemini DNS Radar"
git branch -M main
git remote add origin https://github.com/USERNAME/REPOSITORY.git
git push -u origin main
```

## 6. Добавить GitHub Secrets

В GitHub:

`Repository -> Settings -> Secrets and variables -> Actions -> New repository secret`

Создай два секрета:

### `PUBLISH_URL`

```text
https://gemini-dns-radar.<твой-subdomain>.workers.dev
```

### `PUBLISH_TOKEN`

Тот же токен, который был задан через `wrangler secret put PUBLISH_TOKEN`.

## 7. Первый запуск crawler

В GitHub:

`Actions -> Refresh Gemini DNS ranking -> Run workflow`

После завершения обнови сайт. Дальше workflow запускается каждый час на 7-й минуте:

```yaml
cron: "7 * * * *"
```

GitHub schedule может стартовать с небольшой задержкой — это нормально.

## API

### Публичный рейтинг

```text
GET /api/top
```

### Ручная проверка

```text
GET /api/check?dns=xbox-dns.ru
```

Если DNS уже сканировался — возвращается последний полный DoT-результат. Если новый — выполняется быстрая DoH-проверка и hostname ставится в очередь следующего DoT-скана.

### Внутреннее состояние crawler

```text
GET /api/state
Authorization: Bearer <PUBLISH_TOKEN>
```

### Публикация нового состояния

```text
POST /api/publish
Authorization: Bearer <PUBLISH_TOKEN>
Content-Type: application/json
```

## Как определяется `gemini_likely`

1. Private DNS должен отвечать через TLS на порту 853.
2. Он должен успешно резолвить минимум 3 из 4 Gemini-целей.
3. Минимум для двух целевых доменов DNS должен вернуть хотя бы один IPv4, который не входит в опубликованные IP-сети Google.

Это значительно сильнее, чем простая проверка «домен Gemini резолвится», потому что обычные 1.1.1.1 / 8.8.8.8 не должны автоматически попадать в рейтинг.

## Windows

Современный `pywrangler` поддерживает Windows напрямую. Для самой подробной инструкции открой `START_HERE_WINDOWS.md`. Docker и WSL2 для этого проекта не нужны.
