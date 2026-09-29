# Лаба 2 — Полный мониторинг маленького сервиса

## Часть 1 - Заглушка сервиса

Создана и развернута в namespace `lab2` заглушка API `products` с веб-страницей и сценариями ошибки (`/maintenance-appointments/error`), задержки (`/maintenance-appointments/delay`) и нагрузки (`/maintenance-appointments/load`).

### Запуск

Так как у проекта уже есть развернутый кубер, было принято запускать там, но все таки отдельной заглушкой для отладки
Для варианта с Kubernetes сначала собери образ и отправь его в registry, доступный кластеру. Затем переопредели image values и установи учебный чарт:

```sh
docker build -t gitlab.pochini.online:5050/backend/products:lab2-observability-20260927 .
docker push gitlab.pochini.online:5050/backend/products:lab2-observability-20260927
helm upgrade --install products-observability-demo \
  /Users/paninanq/study/sem7/cloud-infrastructure/lab2/stub/helm \
  --namespace lab2 \
  --set-string image.repository=gitlab.pochini.online:5050/backend/products \
  --set-string image.tag=lab2-observability-20260927 \
  --set-string 'imagePullSecrets[0].name=gitlab-registry' \
  --wait --timeout 5m
```

### Сценарии и телеметрия

| Кнопка / маршрут | Поведение |
| --- | --- |
| `GET /maintenance-appointments/error` | Возвращает 500, увеличивает счётчики запросов и ошибок, помечает текущий спан как ошибочный. |
| `GET /maintenance-appointments/delay` | Ждёт 2 секунды внутри дочернего спана `slow-dependency`. |
| `GET /maintenance-appointments/load` | Параллельно выполняет 25 HTTP-запросов к `GET /autoservices` на этом же сервисе. |
| `GET /metrics/` | Отдаёт Prometheus-метрики: запросы, ошибки и гистограмму длительности ответа. Запрос к `/metrics` перенаправляется на этот адрес. |

Каждый прикладной запрос пишет одну JSON-строку в stdout с уровнем, сообщением и `trace_id`. FastAPI автоматически получает серверный спан через OpenTelemetry; ручной дочерний спан показывает искусственную задержку.

```
kubectl -n lab2 port-forward service/products-observability-demo 8000:8000
Forwarding from 127.0.0.1:8000 -> 8000
Forwarding from [::1]:8000 -> 8000
```

![stub](images/Снимок%20экрана —%202026-09-28%20в 19.12.45.png)
## Часть 2 - Метрики

В задании указан Prometheus, но в проекте уже работает стек VictoriaMetrics. Поэтому для лабы использован Prometheus-совместимый вариант: vmagent собирает метрики заглушки и отправляет их в VictoriaMetrics, а Grafana запрашивает их через Prometheus-совместимый API. Отдельный Prometheus Server не устанавливался, чтобы использовать существующую инфраструктуру и не дублировать хранение метрик.

То есть на момент начала лабы на машине Мониторинга уже был docker compose:

```
services:
  victoriametrics:
    image: ${VICTORIA_METRICS_IMAGE:-victoriametrics/victoria-metrics:latest}
    command:
      - -storageDataPath=/storage
      - -retentionPeriod=${VICTORIA_METRICS_RETENTION:-7d}
    ports:
      # Bind only to the private-network address. VMAgent writes here.
      - ${VICTORIA_METRICS_BIND_ADDRESS:?Set VICTORIA_METRICS_BIND_ADDRESS}:8428:8428
    volumes:
      - victoria-metrics-data:/storage
    restart: unless-stopped

  grafana:
    image: ${GRAFANA_IMAGE:-grafana/grafana:latest}
    depends_on:
      victoriametrics:
        condition: service_started
    environment:
      GF_SECURITY_ADMIN_USER: ${GRAFANA_ADMIN_USER:-admin}
      GF_SECURITY_ADMIN_PASSWORD: ${GRAFANA_ADMIN_PASSWORD:?Set GRAFANA_ADMIN_PASSWORD}
      GF_SERVER_DOMAIN: ${GRAFANA_DOMAIN:-monitoring.pochini.online}
    ports:
      # Bind only to the WireGuard address; never publish Grafana publicly.
      - ${GRAFANA_BIND_ADDRESS:?Set GRAFANA_BIND_ADDRESS}:3000:3000
    volumes:
      - grafana-data:/var/lib/grafana
      - ./grafana/provisioning:/etc/grafana/provisioning:ro
    restart: unless-stopped

volumes:
  victoria-metrics-data:
  grafana-data:
```


`vmagent` выбирает Kubernetes Service по аннотациям. Для заглушки нужен Service с такими метаданными:

```yaml
metadata:
  annotations:
    prometheus.io/scrape: "true"
    prometheus.io/port: "8000"
    prometheus.io/path: "/metrics/"
```

Подготовлен JSON дашборда с панелями RPS, доли ошибок 5xx и p95: [`grafana-dashboard-red.json`](monitoring-infra/grafana/provisioning/dashboards/grafana-dashboard-red.json).

![dashboard](images/Снимок%20экрана —%202026-09-28%20в 19.13.55.png)

 Интенсивность запросов показывает, сколько запросов сервис получает в секунду, и помогает заметить всплески нагрузки. Доля ответов 5xx показывает, какая часть запросов завершилась серверной ошибкой; её рост означает, что пользователи чаще не получают ожидаемый результат. p95 времени ответа показывает границу, быстрее которой выполняются 95% запросов; этот показатель помогает заметить медленные запросы, которые теряются за средним временем ответа.

## Часть 3 — Логи

Для сбора логов в Kubernetes через Helm установлен Grafana Alloy (`grafana/alloy`) в namespace `monitoring`. 

```sh
helm repo add grafana https://grafana.github.io/helm-charts --force-update
helm repo update grafana
helm upgrade --install alloy grafana/alloy \
  --namespace monitoring \
  --create-namespace \
  --values values/alloy.yaml \
  --wait --timeout 10m --rollback-on-failure
```

Alloy находит pod заглушки по метке приложения, добавляет метки `app`, `namespace` и `container`, извлекает `level` из JSON и отправляет записи в Loki.

В `docker compose` на monitoring VM добавлен сервис Loki: 

```yaml
services:
  loki:
    image: ${LOKI_IMAGE:-grafana/loki:3.7.3}
    command:
      - -config.file=/etc/loki/config.yaml
    ports:
      - ${VICTORIA_METRICS_BIND_ADDRESS:?Set VICTORIA_METRICS_BIND_ADDRESS}:3100:3100
    volumes:
      - ./loki/config.yaml:/etc/loki/config.yaml:ro
      - loki-data:/loki
    restart: unless-stopped

  grafana:
    depends_on:
      loki:
        condition: service_started

volumes:
  loki-data:
```

В Grafana найдены JSON-логи запросов к `/maintenance-appointments/delay`

![log](images/Снимок%20экрана —%202026-09-29%20в 11.46.20.png)

Так же например ошибки

![log](images/Снимок%20экрана —%202026-09-29%20в 19.08.11.png)
## Часть 4 — Трейсы

В `docker compose` на monitoring VM добавлен Jaeger all-in-one. OTLP/HTTP порт доступен из Kubernetes по приватной сети, UI доступен только по WireGuard:

```yaml
services:
  jaeger:
    image: ${JAEGER_IMAGE:-jaegertracing/jaeger:latest}
    ports:
      - ${VICTORIA_METRICS_BIND_ADDRESS:?Set VICTORIA_METRICS_BIND_ADDRESS}:4318:4318
      - ${GRAFANA_BIND_ADDRESS:?Set GRAFANA_BIND_ADDRESS}:16686:16686
    restart: unless-stopped
```


Переустановим заглушку с `OTEL_EXPORTER_OTLP_ENDPOINT`:

```sh
helm upgrade --install products-observability-demo \
  ./lab2/stub/helm \
  --namespace lab2 \
  --set-string image.repository=gitlab.pochini.online:5050/backend/products \
  --set-string image.tag=lab2-observability-20260927 \
  --set-string 'imagePullSecrets[0].name=gitlab-registry' \
  --set-string otel.endpoint=http://{OTEL_EXPORTER_OTLP_ENDPOINT}:4318 \
  --wait --timeout 5m --rollback-on-failure
```

Поиск трейсов сервиса `products-observability-demo` в Jaeger:

![Traces](images/Снимок%20экрана —%202026-09-29%20в 12.10.14.png)

Пример трейса c задеожкой:

![Trace](images/Снимок%20экрана —%202026-09-29%20в 12.33.07.png)

Трейс с ошибкой из Loki:

![Trace](images/Снимок%20экрана —%202026-09-29%20в 19.10.55.png)

## Часть 5 — Алерты
Были настроены три правила в [`monitoring-infra/rules/products-demo.yaml`](monitoring-infra/rules/products-demo.yaml): доля ответов 5xx выше 5%, более 20 запросов за 2 минуты и p95 выше 1 секунды. Первое и третье условия должны сохраняться 30 секунд; алерт нагрузки срабатывает сразу.


`vmalert` загружает правила, раз в 15 секунд запрашивает метрики у VictoriaMetrics и отправляет сработавшие алерты в Alertmanager. Alertmanager группирует их и отправляет webhook в `alert-webhook`.

```yaml
services:
  alert-webhook:
    image: ${ALERT_WEBHOOK_IMAGE:-python:3.13-slim}
    command: [python, -u, /app/alert-webhook.py]
    volumes:
      - ./scripts/alert-webhook.py:/app/alert-webhook.py:ro
    restart: unless-stopped

  alertmanager:
    image: ${ALERTMANAGER_IMAGE:-prom/alertmanager:latest}
    depends_on:
      alert-webhook:
        condition: service_started
    command:
      - --config.file=/etc/alertmanager/alertmanager.yaml
      - --web.listen-address=0.0.0.0:9093
    ports:
      - ${GRAFANA_BIND_ADDRESS:?Set GRAFANA_BIND_ADDRESS}:9093:9093
    volumes:
      - ./alertmanager/alertmanager.yaml:/etc/alertmanager/alertmanager.yaml:ro
    restart: unless-stopped

  vmalert:
    image: ${VMALERT_IMAGE:-victoriametrics/vmalert:latest}
    depends_on:
      victoriametrics:
        condition: service_started
      alertmanager:
        condition: service_started
    command:
      - -rule=/etc/vmalert/rules/*.yaml
      - -datasource.url=http://victoriametrics:8428
      - -remoteRead.url=http://victoriametrics:8428
      - -remoteWrite.url=http://victoriametrics:8428
      - -notifier.url=http://alertmanager:9093
      - -evaluationInterval=15s
    volumes:
      - ./rules:/etc/vmalert/rules:ro
    restart: unless-stopped
```
Настроены три правила. 

Алерт на ошибки ProductsDemoHighErrorRate срабатывает, если доля ответов 5xx за последние 5 минут превышает 5% и условие сохраняется 30 секунд. Такой рост означает, что значимая часть запросов завершается ошибкой и пользователи могут не выполнить нужное действие. 

![alert](images/Снимок%20экрана —%202026-09-29%20в 18.51.03.png)

Алерт на задержку ProductsDemoHighP95Latency срабатывает, если p95 времени ответа за последние 5 минут превышает 1 секунду в течение 30 секунд. Это сигнал, что заметная доля запросов обрабатывается медленно, что ухудшает пользовательский опыт.

![alert](images/Снимок%20экрана —%202026-09-29%20в 18.47.35.png)

Алерт на нагрузку ProductsDemoTrafficBurst срабатывает, если за последние 2 минуты поступило более 20 запросов. Он помогает заметить резкий рост нагрузки, который может привести к нехватке ресурсов и замедлению сервиса. 

![alert](images/Снимок%20экрана —%202026-09-29%20в 18.25.24.png)
