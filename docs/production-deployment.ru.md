# Продакшен-развёртывание независимого AIMarket Hub

[English](production-deployment.md) · [Русский](production-deployment.ru.md) · [Español](production-deployment.es.md) · [Français](production-deployment.fr.md) · [中文](production-deployment.zh.md)

> Терминология соответствует каноническому [глоссарию локализации](https://github.com/alexar76/aicom/blob/main/docs/localization-glossary.md). Названия продуктов, идентификаторы протокола, URL, CLI-команды и переменные окружения не переводятся.

Это руководство предназначено для независимого оператора, который разворачивает публичный хаб и хочет пройти допуск в федерацию без ручного обхода доверия. Описана установка на один сервер Ubuntu: nginx, TLS, необязательный Alien Monitor, платная подписанная capability и необязательное подключение нод-агента SKOPOS.

## 1. Целевая архитектура и граница доверия

```text
Интернет
  └─ nginx :80/:443
       ├─ /hub/      → AIMarket Hub на 127.0.0.1:9083
       ├─ /monitor/  → Alien Monitor на 127.0.0.1:9100 (необязательно)
       └─ provider   → backend capability на 127.0.0.1:<port>, не публичный
```

Для каждого сервиса создайте отдельного непривилегированного Unix-пользователя. Снаружи должны слушать только nginx и SSH. Базы SQLite и ключи Ed25519 храните вне Git checkout. Публичный origin хаба — граница безопасности: discovery, подписанный манифест и объявленный `invoke` обязаны использовать этот же origin.

Проще всего выделить имя `hub.example.com`. Подпуть портала, например `https://example.com/hub`, тоже поддерживается, но требует всех proxy-правил из §7. Пропуск одного правила часто оставляет API рабочим, но ломает ресурсы UI, WebSocket или URL федерации.

## 2. Требования

- Ubuntu 24.04 LTS или другой поддерживаемый Linux с systemd.
- Публичный IPv4/IPv6 и уже указывающие на сервер DNS-записи `A`/`AAAA`.
- Действующий домен под вашим контролем. Не запрашивайте TLS, пока DNS не разрешается глобально.
- SSH-доступ по ключу Ed25519. Не закрывайте первый сеанс, пока не проверите второй вход только по ключу.
- Не менее 2 CPU, 2 ГБ RAM и 10 ГБ свободного диска для Hub + Monitor.
- Исходящий HTTPS для Git, ACME, обхода федерации и подключения SKOPOS.

```bash
sudo apt update
sudo apt install -y git nginx python3 python3-venv python3-pip \
  certbot python3-certbot-nginx sqlite3 ufw fail2ban unattended-upgrades
```

## 3. Закрепление ревизий исходного кода

Не разворачивайте изменяемую ветку напрямую. Запишите и проверьте commit SHA, затем переключитесь строго на него:

```bash
sudo install -d -m 0755 /opt/aimarket/releases
sudo git clone https://github.com/alexar76/aimarket-hub.git \
  /opt/aimarket/releases/aimarket-hub-<sha>
sudo git -C /opt/aimarket/releases/aimarket-hub-<sha> checkout --detach <sha>
git -C /opt/aimarket/releases/aimarket-hub-<sha> rev-parse HEAD
```

Повторите для `alexar76/alien-monitor` и `alexar76/skopos`, если они нужны. Сохраните SHA в журнале развёртывания. Если поверх ревизии нужен hardening-патч, храните патч и чистый исходник вне checkout; `git status --short` должен показывать только намеренные изменения.

```bash
python3 -m venv /opt/aimarket/venvs/hub
/opt/aimarket/venvs/hub/bin/pip install --upgrade pip
/opt/aimarket/venvs/hub/bin/pip install \
  /opt/aimarket/releases/aimarket-hub-<sha>
```

Перед вводом в эксплуатацию прогоните тесты репозитория на точной ревизии в CI или на build-host. Не оставляйте на продакшен-сервере компиляторы и dev-зависимости без необходимости.

## 4. Пользователи, каталоги и права

```bash
sudo useradd --system --home /nonexistent --shell /usr/sbin/nologin aimarket-hub
sudo useradd --system --home /nonexistent --shell /usr/sbin/nologin aimarket-provider
sudo useradd --system --home /nonexistent --shell /usr/sbin/nologin alien-monitor

sudo install -d -o aimarket-hub -g aimarket-hub -m 0750 /var/lib/aimarket/hub
sudo install -d -o aimarket-provider -g aimarket-provider -m 0750 /var/lib/aimarket/provider
sudo install -d -o root -g root -m 0700 /etc/aimarket
sudo install -d -o root -g root -m 0700 /var/backups/aimarket
```

- Ключ подписи хаба: `0600`, владелец `aimarket-hub`.
- Ключ подписи поставщика: `0600`, владелец `aimarket-provider`.
- Файлы окружения: `0600`, владелец root.
- Каталоги SQLite доступны на запись только своему сервису.
- Резервные копии с ключами: `0600` внутри каталога `0700`.

Никогда не помещайте закрытый ключ, admin token, enrollment ticket или пароль БД в Git, аргументы процесса, историю shell или публичный лог поддержки.

## 5. Конфигурация хаба

Создайте `/etc/aimarket/hub.env`:

```dotenv
AIMARKET_HUB_NAME=Example Independent Hub
AIMARKET_HUB_URL=https://example.com/hub
AIMARKET_BIND_HOST=127.0.0.1
AIMARKET_DB_PATH=/var/lib/aimarket/hub/hub.db
AIMARKET_SIGNING_KEY_PATH=/var/lib/aimarket/hub/hub_signing_key
AIFACTORY_PROD=1
# Доверяйте forwarded-адресам только от локального nginx; никогда не используйте *.
AIMARKET_TRUSTED_PROXIES=127.0.0.1,::1
# Независимый оператор не должен зависеть от oracle другого оператора.
AIMARKET_ORACLE_FAMILY_URL=off

AIMARKET_AUTO_CRAWL=1
AIMARKET_CRAWL_INTERVAL_S=3600
AIMARKET_CRAWL_REFRESH_MAX=64
AIMARKET_SEED_LIST=https://modelmarket.dev/.well-known/ai-market.json
# Закрепите ключ из подписанного seed well-known после независимой проверки.
AIMARKET_SEED_PUBKEYS=https://modelmarket.dev/.well-known/ai-market.json=<seed-ed25519-public-key>

AIMARKET_FEDERATION_ASSAY=1
AIMARKET_FEDERATION_ASSAY_SANDBOX=1
AIMARKET_FEDERATION_AUTO_ADMIT=1

# Продакшен-платёж без блокчейна, который действительно применяет 402.
AIMARKET_CREDITS_ENABLED=1
AIMARKET_CREDITS_OPEN_SIGNUP=1
AIMARKET_CREDITS_FREE_GRANT_USD=0

AIMARKET_ADMIN_TOKEN=<secret>
AIMARKET_PUBLISHER_TOKENS=<publisher-id>:<secret>
AIMARKET_ECOSYSTEM_LABELS=publisher-id=Display Name
AIMARKET_CORS_ORIGINS=https://example.com
```

Важные правила:

1. `AIMARKET_HUB_URL` — внешний URL, включая `/hub` при работе на подпути. В продакшене это не `localhost`.
2. При первом запуске хаб создаёт ключ Ed25519. Сразу сделайте резервную копию. Замена ключа меняет идентичность в федерации и для пиров с закреплённым ключом выглядит как захват.
3. Seed key публичен, но закрепление ключа — решение о доверии. Получите его из подписанного well-known по проверенному TLS и запишите результат сверки.
4. При отсутствии admin token операторские маршруты должны fail-closed (отказ по умолчанию).
5. При SQLite останавливайте хаб или используйте SQLite backup API. Для нескольких процессов и большой записи предпочтителен PostgreSQL.
6. Выдавайте каждому поставщику отдельный subject-scoped publisher token. Не используйте admin token хаба как credential поставщика. Для нескольких модулей этого хаба задайте `AIMARKET_ECOSYSTEM_LABELS=publisher-id=Display Name,...`: подписанное расширение `ecosystem.nodes` покажет их принадлежность, не выдавая их за независимые федеративные пиры.
7. На каждом цикле crawler должен в пределах лимита обновлять уже доверенные активные пиры, даже если текущий seed graph больше на них не ссылается. Подберите `AIMARKET_CRAWL_REFRESH_MAX` под размер roster; иначе валидный пир с 30 capability может бесконечно оставаться в индексе со старым манифестом на одну capability.

## 6. Сервис systemd

Создайте `/etc/systemd/system/aimarket-hub.service`:

```ini
[Unit]
Description=Independent AIMarket Hub
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=aimarket-hub
Group=aimarket-hub
WorkingDirectory=/opt/aimarket/releases/aimarket-hub-<sha>
EnvironmentFile=/etc/aimarket/hub.env
ExecStart=/opt/aimarket/venvs/hub/bin/python -m aimarket_hub serve
Restart=on-failure
RestartSec=5s
NoNewPrivileges=yes
PrivateTmp=yes
ProtectSystem=strict
ProtectHome=yes
ProtectKernelTunables=yes
ProtectKernelModules=yes
ProtectControlGroups=yes
RestrictSUIDSGID=yes
RestrictNamespaces=yes
LockPersonality=yes
RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6
ReadWritePaths=/var/lib/aimarket/hub
UMask=0027
MemoryMax=1G
TasksMax=256

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now aimarket-hub
sudo systemctl status aimarket-hub --no-pager
```

Привяжите приложение к loopback, если релиз поддерживает bind-host. Если оно слушает `0.0.0.0:9083`, UFW всё равно должен запрещать этот порт, а nginx должен оставаться единственным публичным путём.

## 7. nginx, подпути и TLS

Базовая конфигурация для подпути; адаптируйте сертификаты и зоны rate limit:

```nginx
server {
    listen 80;
    listen [::]:80;
    server_name example.com;
    location ^~ /.well-known/acme-challenge/ { root /var/www/letsencrypt; }
    location / { return 301 https://$host$request_uri; }
}

server {
    listen 443 ssl http2;
    listen [::]:443 ssl http2;
    server_name example.com;
    ssl_certificate /etc/letsencrypt/live/example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/example.com/privkey.pem;
    ssl_protocols TLSv1.2 TLSv1.3;
    ssl_session_tickets off;
    add_header Strict-Transport-Security "max-age=31536000; includeSubDomains" always;
    add_header X-Content-Type-Options nosniff always;
    add_header Referrer-Policy strict-origin-when-cross-origin always;
    client_max_body_size 1m;

    location = /hub { return 301 /hub/; }
    location ^~ /hub/ {
        proxy_pass http://127.0.0.1:9083/;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto https;
        proxy_read_timeout 120s;
    }

    # Текущий UI использует несколько root-relative маршрутов.
    location ~ ^/(ai-market|api|mcp|developers|examples|widget|plugins|operator|studio)(/|$) {
        proxy_pass http://127.0.0.1:9083;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-Proto https;
    }
    location = /hub-ui-i18n.json { proxy_pass http://127.0.0.1:9083/hub-ui-i18n.json; }
    location = /cap-descriptions-i18n.json { proxy_pass http://127.0.0.1:9083/cap-descriptions-i18n.json; }
}
```

Для Alien Monitor соберите frontend с `VITE_BASE_PATH=/monitor/`, отдельно проксируйте `/monitor/` на loopback HTTP-порт, а для `/monitor/ws` задайте WebSocket-заголовки `Upgrade`/`Connection` и большой read timeout. Сборка для `/` может отвечать HTTP 200, но терять JavaScript и CSS на подпути.

```bash
sudo nginx -t
sudo systemctl reload nginx
sudo certbot --nginx -d example.com
sudo certbot renew --dry-run
systemctl is-enabled certbot.timer
```

Не включайте HSTS, пока HTTPS не работает на всех охватываемых поддоменах.

Если на корне домена работает отдельный портал, запускайте его ещё одним непривилегированным loopback-сервисом и проксируйте только как последний fallback nginx. Префиксы API портала, например `/api/v1/`, должны стоять раньше широкого compatibility-правила хаба для root-relative маршрутов; иначе страница откроется, а API незаметно уйдёт в хаб. Оставьте `/hub/`, `/monitor/` и provider paths явными и не перенаправляйте `/` на `/hub/`, когда точкой входа должен быть портал.

## 8. Реальная capability без обхода оплаты

Для первой локальной smoke-capability остановите хаб и запустите идемпотентный quickstart от пользователя хаба. Он создаёт исполняемый static pack без модели и внешнего поставщика; прежде чем предлагать его как продукт, замените его своим backend:

```bash
sudo systemctl stop aimarket-hub
sudo -u aimarket-hub env \
  AIMARKET_DB_PATH=/var/lib/aimarket/hub/hub.db \
  AIMARKET_HUB_URL=https://example.com/hub \
  AIMARKET_CREDITS_ENABLED=1 \
  /opt/aimarket/venvs/hub/bin/python -m aimarket_hub quickstart \
  --price 0.01 --grant 0
sudo systemctl start aimarket-hub
```

Созданный buyer key — секрет, который показывается один раз. При нулевом grant на нём нет баланса; сохраните его в password manager или отзовите. Для реального поставщика следуйте руководству [«Как запустить собственный хаб»](operator-quickstart.md), регистрируйте только контролируемый вами backend и сохраняйте ключ подписи поставщика.

У продакшен-capability две поверхности:

- внутренний backend поставщика, который выполняет работу и подписывает результат;
- публичный gateway хаба, который проверяет безопасность, оплату, маршрутизацию и квитанции.

Подписанный манифест должен объявлять gateway хаба, например `https://example.com/hub/ai-market/v2/invoke`, а не прямой `/provider/invoke`. Ограничьте маршрут поставщика loopback, взаимной аутентификацией или собственным адресом хоста. Релиз, публикующий URL поставщика в манифесте, нельзя выпускать без обновления или проверенного backport.

Для платной capability запрос без авторизации, в том числе с `X-AIMarket-Sandbox-Visitor`, обязан вернуть `402`, назвать пригодный платёжный канал и указать ровно ту цену, что записана в подписанном каталоге. Альтернатива — действительно бесплатная capability, которая выполняет работу и возвращает подписанную квитанцию. Демонстрационный endpoint без доказательства не подходит.

Минимальные проверки:

- `/hub/.well-known/ai-market.json` соответствует поставляемой схеме;
- `manifest_url` публичен и same-origin;
- подписи well-known и манифеста проверяются объявленным ключом Ed25519;
- `generated_at` свежий;
- канонические пять полей манифеста охватывают число capability, timestamp, protocol version, `tools_hash` и `by_hub_hash`;
- каждое числовое поле схемы — число, а не `null`;
- объявленный `invoke_url` same-origin и ведёт в gateway хаба;
- цена в `402` совпадает с `price_per_call_usd`;
- успешный ответ поставщика содержит проверяемую подпись.

## 9. Допуск в федерацию

После локальных проверок выполните публичный announce:

```bash
curl -fsS -X POST https://modelmarket.dev/ai-market/v2/federation/announce \
  -H 'Content-Type: application/json' \
  --data '{
    "hub_url":"https://example.com/hub",
    "hub_name":"Example Independent Hub",
    "well_known_url":"https://example.com/hub/.well-known/ai-market.json"
  }'

curl -fsS 'https://modelmarket.dev/ai-market/v2/federation/assay?url=https%3A%2F%2Fexample.com%2Fhub'
```

```text
announce или входящий crawl → pending/quarantine → sandbox assay → pass → trusted/indexed
```

Не обходите доверие через operator panel, admin endpoint или ручное редактирование peer table. Известный pending-пир может ждать следующего цикла обхода принимающего хаба; часовой интервал нормален. При `review` или `fail` исправьте живое доказательство и дождитесь штатного повторного запуска. Сохраните dossier: в нём указана проваленная проверка.

После допуска проверяйте roster пиров, результат федеративного поиска и Alien Monitor по имени и `capability_id`, а не только по HTTP 200.

## 10. Безопасность сервера

```bash
sudo ufw default deny incoming
sudo ufw default allow outgoing
sudo ufw limit 22/tcp
sudo ufw allow 80/tcp
sudo ufw allow 443/tcp
sudo ufw enable
sudo ufw status verbose
```

Включите Fail2ban jails для `sshd`, `nginx-http-auth` и `nginx-botsearch`, а также автоматические security-upgrades. Регулярно проверяйте bans и журналы обновлений: сама установка ещё не является мониторингом.

Критический порядок укрепления SSH:

1. Установите публичный ключ оператора.
2. Откройте второй сеанс с `BatchMode=yes` и нужным ключом.
3. Проверьте `sshd -T`: ранний drop-in от cloud-init может перекрыть визуально более поздний файл.
4. Задайте `PasswordAuthentication no`, `KbdInteractiveAuthentication no`, `PermitRootLogin prohibit-password` или полностью отключите root и используйте sudo.
5. Перезагрузите конфигурацию SSH, оставьте первый сеанс открытым и проверьте третий вход только по ключу.

Не закрывайте единственный рабочий сеанс до независимой проверки.

## 11. Резервные копии и восстановление

Сохраняйте как минимум:

- основную SQLite БД и отдельные базы каналов/provenance;
- закрытые ключи Ed25519 хаба и поставщика;
- root-owned env-файлы и systemd drop-ins;
- конфигурацию nginx;
- закреплённые SHA и файлы намеренных патчей.

Используйте `sqlite3 /path/hub.db '.backup /staging/hub.db'` или остановите сервис перед копированием. Создавайте root-only архив, храните несколько поколений и выносите зашифрованную копию с сервера. Резервная копия не проверена, пока вы не выполнили восстановление.

Восстановление:

1. Остановите сервисы хаба и поставщика.
2. Верните базы и ключи с исходными владельцами и режимами.
3. Верните конфигурацию, выполните `systemctl daemon-reload` и `nginx -t`.
4. Запустите сервисы и убедитесь, что публичный ключ подписанта не изменился.
5. Повторите проверки схемы, подписи, `402`, поиска и федерации.

## 12. Alien Monitor и SKOPOS

Запускайте Alien Monitor отдельным непривилегированным сервисом в LIVE-режиме. Укажите локальный URL хаба, раздельно проксируйте HTTP API и WebSocket и после reboot убедитесь, что tick продолжает расти. Пустые финансовые метрики могут означать отсутствие трафика; замерший tick или неработающий health endpoint — авария.

Считайте каждый независимый Hub отдельной звёздной системой. Удаляйте дубли по стабильной идентичности и нормализованному URL, задайте минимальное расстояние между центрами хабов, а `ecosystem.nodes` группируйте вокруг их владельца. Сначала резервируйте child budget хаба для подписанных `ecosystem.nodes`, затем рисуйте связи с федеративными пирами — иначе большой roster скроет собственных поставщиков. ИИ-помощник должен сначала искать имя в текущем live snapshot, включая локализованные и транслитерированные запросы, и лишь затем переходить к статической справке; так новые модули видны и без внешнего LLM key.

SKOPOS использует ограниченный push-only нод-агент. Для подключения нужен официальный одноразовый ticket:

1. Выпустите ticket в control plane SKOPOS для точной записи сервера.
2. Запустите сгенерированный installer на ноде от root.
3. Введите ticket через скрытый stdin. Не передавайте его в argv, URL, историю shell или чат.
4. Подтвердите, что ticket погашен, credential file доступен только root/агенту, а timers успешно отправляют отчёты.

Нода не может выпустить ticket сама: он создаётся только в control plane SKOPOS. Если ticket отсутствует, оставьте подготовленный installer остановленным и явно отметьте enrollment как pending; не угадывайте ticket и не копируйте его с другой ноды.

Не создавайте credential вручную в БД SKOPOS и не добавляйте агента в группу Docker и unrestricted sudo.

## 13. Финальная приёмка

```bash
systemctl is-active aimarket-hub nginx fail2ban ufw unattended-upgrades
systemctl --failed
curl -fsS https://example.com/hub/.well-known/ai-market.json
curl -fsS https://example.com/hub/ai-market/v2/manifest

# Платный вход должен вернуть 402 с ценой из каталога.
curl -sS -o /tmp/invoke.json -w '%{http_code}\n' \
  -H 'Content-Type: application/json' \
  --data '{"product_id":"example","capability_id":"example.echo@v1","input":{}}' \
  https://example.com/hub/ai-market/v2/invoke

ufw status verbose
sshd -T | egrep '^(permitrootlogin|passwordauthentication|kbdinteractiveauthentication) '
```

Перезагрузите сервер и повторите проверки из внешней сети. Приёмка успешна, если:

- все сервисы запускаются автоматически;
- публичны только SSH, HTTP и HTTPS;
- dry-run продления TLS успешен;
- подписи проверяются, ключ подписанта не изменился;
- публичный backend поставщика нельзя использовать в обход gateway;
- хаб остаётся доступен в поиске федерации;
- Alien Monitor переподключился, LIVE tick растёт;
- свежая резервная копия существует, процедура восстановления задокументирована.

## 14. Обновление и откат

1. Создайте новый неизменяемый release-каталог и virtualenv; не выполняйте `git pull` в live-release.
2. Изучите миграции, особенно изменения путей SQLite channel/provenance.
3. Сохраните базы и ключи подписи.
4. Запустите тесты и локальный federation assay для кандидата.
5. Измените только target `WorkingDirectory`/`ExecStart`, перезапустите сервис и повторите приёмку.
6. При необходимости откатите код, но не откатывайте БД через несовместимую миграцию без соответствующей резервной копии.

Идентичность Ed25519 и платёжный ledger — постоянное состояние, а не артефакты релиза. Их потеря или незаметная замена не являются обычным повторным развёртыванием.
