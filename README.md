# Konnex multi-wallet miner (OpenRouter)

Майнер для сабнета `knx-subnet-drone-navigation`, использующий **OpenRouter** как
бэкенд инференса и поддерживающий **до 1000 кошельков** в одном процессе.

## Архитектура

- Один Python-процесс, общий `subtensor` + `metagraph` + `aiohttp`-сессия.
- Для каждого hotkey-а поднимается отдельный `bt.axon` на своём порту
  (`AXON_PORT_BASE`, `AXON_PORT_BASE+1`, …).
- Все аксоны разделяют общие `forward / blacklist / priority` (зависят только от
  общей метаграфы).
- На каждый запрос валидатора майнер запускает 3 кандидата к OpenRouter
  (температуры 0.2 / 0.8 / 0.8) **параллельно** и отдаёт того, у кого
  `confidence` максимальный — это совпадает с логикой оригинального майнера.
- При ошибке OpenRouter — фолбэк на эвристику по ключевым словам инструкции.

## Развёртывание (Linux VPS, Ubuntu 22.04+)

```bash
git clone <this repo>
cd konnex_node
chmod +x install.sh run.sh
./install.sh
```

Затем:

1. Положить 1000 сид-фраз в `mnemonics.txt` (по одной на строку).
2. Отредактировать `.env`:
   - `OPENROUTER_API_KEY` — ключ OpenRouter
   - `EXTERNAL_IP` — публичный IP VPS
   - `NETUID`, `SUBTENSOR_CHAIN_ENDPOINT` — параметры сабнета
   - `AXON_PORT_BASE` — стартовый порт (по умолчанию 8091)
3. Открыть TCP-порты `AXON_PORT_BASE … AXON_PORT_BASE+N-1` на firewall VPS.
4. Восстановить кошельки и зарегистрировать их в сабнете:
   ```bash
   ./run.sh bootstrap
   ```
   Скрипт читает `mnemonics.txt`, для каждой фразы создаёт `coldkey` и `hotkey`
   (оба регенерируются из той же seed-фразы), регистрирует hotkey через
   `btcli subnet register`, делает паузу `REGISTER_DELAY_SECONDS` между
   регистрациями, пишет манифест `wallets.json`.
5. Запустить майнер:
   ```bash
   ./run.sh miner
   ```

## Системные требования

- Ubuntu 22.04+, Python 3.11.
- ~1.5–2 GB RAM на сам процесс + по ~3–5 MB на каждый axon (1000 хоткей ≈
  4–6 GB RAM суммарно). Для 1000 кошельков рекомендую VPS с 8+ GB RAM.
- Открытые TCP-порты по числу хоткей.
- Лимит file descriptors: `ulimit -n 65535` (систему/systemd).

## Конфигурация OpenRouter

- `OPENROUTER_MODEL=openai/gpt-4o-mini` — по умолчанию (как договорились).
  Можно поменять на любой OpenRouter-слаг.
- `OPENROUTER_CONCURRENCY=200` — глобальный семафор на одновременные
  запросы к OpenRouter (через 3 кандидата на запрос).
- `OPENROUTER_TIMEOUT_SECONDS=45`.

## Стоимость регистрации

Регистрация 1000 hotkey-ов на сабнете требует TAO (комиссия burn registration).
Bootstrap-скрипт регистрирует последовательно с задержкой `REGISTER_DELAY_SECONDS`
(по умолчанию 12 секунд) — это нужно, чтобы не упереться в rate-limit чейна
и в ImmunityPeriod регистраций. Бюджет в TAO планируйте заранее.

## Файлы

```
miner/
  protocol.py          # DroneNavSynapse (контракт сабнета — байт-в-байт upstream)
  policy_io.py         # action labels + хелперы из openfly_policy_io
  openrouter_client.py # async-клиент OpenRouter, 3 кандидата, фолбэк
  multi_miner.py       # запуск N axon'ов в одном процессе
scripts/
  bootstrap_wallets.py # восстановление + регистрация всех hotkey-ов
install.sh             # установка зависимостей на Ubuntu
run.sh                 # запуск bootstrap | miner
.env.example           # пример конфига
mnemonics.example.txt  # формат файла с сид-фразами
```
