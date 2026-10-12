# Gemini DNS Radar — запуск и публикация на Windows

## 0. Что понадобится

- Windows 10/11
- аккаунт Cloudflare
- аккаунт GitHub
- Node.js
- uv
- Git

## 1. Распакуй архив

Например в:

```text
C:\Projects\gemini-dns-radar
```

Открой PowerShell в этой папке.

## 2. Проверь инструменты

```powershell
node -v
npm -v
git --version
uv --version
```

Если `uv` отсутствует:

```powershell
winget install --id=astral-sh.uv -e
```

Закрой и снова открой PowerShell, затем снова выполни:

```powershell
uv --version
```

Если Node.js отсутствует, установи LTS-версию Node.js, затем снова открой PowerShell.

## 3. Войди в Cloudflare

```powershell
npx wrangler login
```

Откроется браузер. Разреши Wrangler доступ к аккаунту Cloudflare.

## 4. Локальный запуск сайта

Из папки проекта:

```powershell
uvx --from workers-py pywrangler dev
```

После запуска открой адрес, который покажет терминал, обычно:

```text
http://localhost:8787
```

До первого crawler-сканирования TOP-10 будет пустым — это нормально.

Для остановки нажми:

```text
Ctrl+C
```

## 5. Разверни Worker в Cloudflare

```powershell
uvx --from workers-py pywrangler deploy
```

Cloudflare создаст Worker и KV namespace `STATE` автоматически.

Сохрани адрес Worker, например:

```text
https://gemini-dns-radar.ABC.workers.dev
```

Проверь его в браузере.

## 6. Создай PUBLISH_TOKEN

Сгенерируй токен:

```powershell
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

Если команда `python` не установлена, можно выполнить:

```powershell
uv run python -c "import secrets; print(secrets.token_urlsafe(48))"
```

Скопируй получившуюся длинную строку. НЕ публикуй её.

Теперь добавь её как секрет Worker:

```powershell
npx wrangler secret put PUBLISH_TOKEN
```

Когда терминал попросит значение — вставь токен и нажми Enter.

## 7. Создай пустой GitHub-репозиторий

На github.com:

1. New repository.
2. Название, например `gemini-dns-radar`.
3. Не добавляй README/.gitignore/license, потому что они уже есть в архиве.
4. Create repository.

Скопируй HTTPS URL репозитория, например:

```text
https://github.com/USERNAME/gemini-dns-radar.git
```

## 8. Залей проект в GitHub

В PowerShell в папке проекта:

```powershell
git init
git add .
git commit -m "Initial Gemini DNS Radar"
git branch -M main
git remote add origin https://github.com/USERNAME/gemini-dns-radar.git
git push -u origin main
```

Вместо URL выше вставь URL своего репозитория.

## 9. Добавь GitHub Secrets

Открой репозиторий:

```text
Settings
→ Secrets and variables
→ Actions
→ Secrets
→ New repository secret
```

Создай `PUBLISH_URL`.

Значение — адрес Worker БЕЗ завершающего `/`, например:

```text
https://gemini-dns-radar.ABC.workers.dev
```

Создай второй секрет `PUBLISH_TOKEN`.

Значение — тот же токен, который вводил в:

```powershell
npx wrangler secret put PUBLISH_TOKEN
```

## 10. Запусти первый DNS-скан вручную

В GitHub открой:

```text
Actions
→ Refresh Gemini DNS ranking
→ Run workflow
→ Run workflow
```

Открой запущенный workflow и дождись зелёной галочки.

Crawler:

- скачает публичные списки DNS;
- соберёт кандидатов;
- подключится к кандидатам через DNS-over-TLS порт 853;
- проверит Gemini-домены;
- рассчитает score;
- отправит состояние в Cloudflare Worker;
- сформирует TOP-10.

## 11. Проверь сайт

Открой снова:

```text
https://gemini-dns-radar.ABC.workers.dev
```

Теперь в блоке TOP-10 должны появиться найденные рабочие кандидаты, если crawler нашёл DNS с нужным SmartDNS-сигналом.

API можно проверить напрямую:

```text
https://gemini-dns-radar.ABC.workers.dev/api/top
```

## 12. Автоматическое обновление

Ничего больше запускать вручную не нужно.

Файл:

```text
.github/workflows/refresh.yml
```

содержит:

```yaml
cron: "7 * * * *"
```

GitHub Actions будет автоматически запускать crawler каждый час примерно на 7-й минуте часа.

## Если TOP-10 пустой

Открой последний GitHub Actions run и раскрой шаг `Run crawler`.

В логе будут строки вроде:

```text
candidates=...
scanning=...
```

и ошибки отдельных источников/DNS.

Пустой список не обязательно означает ошибку программы: сервис специально не добавляет обычные публичные DNS только ради заполнения десятки. DNS должен пройти SmartDNS-проверку.

## Если `pywrangler` не запускается

Обнови инструменты:

```powershell
uv self update
npm install -g wrangler@latest
```

И снова:

```powershell
uvx --from workers-py pywrangler dev
```

## Что НЕ надо делать

- Docker не нужен.
- MySQL/PostgreSQL не нужны.
- D1 не нужна.
- Постоянно держать компьютер включённым не надо.
- Python crawler локально каждый час запускать не надо — это делает GitHub Actions.
