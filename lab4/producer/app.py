from __future__ import annotations

import asyncio
from collections import deque
from contextlib import asynccontextmanager
from datetime import datetime
import json
import logging
import os
import random
import uuid

from aiokafka import AIOKafkaProducer
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("requests-producer")

KAFKA_BOOTSTRAP = os.getenv("KAFKA_BOOTSTRAP_SERVERS", "localhost:9092")
KAFKA_TOPIC = os.getenv("KAFKA_TOPIC", "repair-requests")
INSTANCE_NAME = os.getenv("INSTANCE_NAME", "producer-1")
PORT = int(os.getenv("PORT", "8001"))

producer: AIOKafkaProducer | None = None
is_connected = False
recent_sent: deque[dict] = deque(maxlen=50)

CARS = ["Lada Vesta", "Toyota Camry", "Kia Rio", "Volkswagen Polo", "BMW 3 Series", "Geely Coolray"]
ISSUES = ["Замена масла и фильтров", "Диагностика тормозной системы", "Стук в передней подвеске", "Замена свечей зажигания", "Ремонт генератора", "Шиномонтаж и балансировка"]


async def init_kafka_producer():
    global producer, is_connected
    while True:
        try:
            logger.info("Connecting to Kafka at %s...", KAFKA_BOOTSTRAP)
            p = AIOKafkaProducer(bootstrap_servers=KAFKA_BOOTSTRAP)
            await p.start()
            producer = p
            is_connected = True
            logger.info("Kafka producer connected successfully to %s", KAFKA_BOOTSTRAP)
            break
        except Exception as e:
            logger.warning("Kafka not ready yet (%s). Retrying in 2 seconds...", e)
            await asyncio.sleep(2)


@asynccontextmanager
async def lifespan(app: FastAPI):
    connect_task = asyncio.create_task(init_kafka_producer())
    yield
    if producer:
        logger.info("Stopping Kafka producer...")
        await producer.stop()
    connect_task.cancel()


app = FastAPI(title="Почини.Онлайн - Сервис создания заявок", lifespan=lifespan)


class RequestCreate(BaseModel):
    car: str | None = None
    issue: str | None = None


@app.post("/api/requests")
async def create_request(data: RequestCreate):
    if not producer or not is_connected:
        raise HTTPException(status_code=503, detail="Kafka producer еще не подключен")

    request_id = f"REQ-{uuid.uuid4().hex[:6].upper()}"
    car = data.car or random.choice(CARS)
    issue = data.issue or random.choice(ISSUES)
    payload = {
        "request_id": request_id,
        "car": car,
        "issue": issue,
        "created_by": INSTANCE_NAME,
        "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }

    payload_bytes = json.dumps(payload).encode("utf-8")

    try:
        # Отправляем без явного ключа, чтобы сообщения распределялись round-robin по партициям
        record_metadata = await producer.send_and_wait(KAFKA_TOPIC, payload_bytes)
    except Exception as e:
        logger.error("Failed to send message: %s", e)
        raise HTTPException(status_code=500, detail=f"Ошибка отправки в Kafka: {e}")

    result = {
        **payload,
        "partition": record_metadata.partition,
        "offset": record_metadata.offset,
        "topic": record_metadata.topic,
    }
    recent_sent.appendleft(result)
    logger.info("Sent %s -> topic=%s, partition=%s, offset=%s", request_id, record_metadata.topic, record_metadata.partition, record_metadata.offset)
    return result


@app.get("/api/requests")
async def list_recent_requests():
    return {
        "connected": is_connected,
        "instance": INSTANCE_NAME,
        "topic": KAFKA_TOPIC,
        "requests": list(recent_sent),
    }


@app.get("/", response_class=HTMLResponse)
async def index():
    return f"""<!DOCTYPE html>
<html lang="ru">
<head>
  <meta charset="UTF-8">
  <title>Почини.Онлайн — Создание заявки (Издатель)</title>
  <style>
    body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; margin: 0; padding: 24px; background: #f5f7fa; color: #2d3748; }}
    .container {{ max-width: 900px; margin: 0 auto; }}
    .header {{ background: #fff; padding: 20px 24px; border-radius: 12px; box-shadow: 0 2px 4px rgba(0,0,0,0.06); margin-bottom: 20px; display: flex; justify-content: space-between; align-items: center; }}
    .badge {{ display: inline-block; padding: 4px 10px; border-radius: 6px; font-size: 13px; font-weight: 600; }}
    .badge-ok {{ background: #c6f6d5; color: #22543d; }}
    .badge-wait {{ background: #feebc8; color: #744210; }}
    .card {{ background: #fff; padding: 24px; border-radius: 12px; box-shadow: 0 2px 4px rgba(0,0,0,0.06); margin-bottom: 20px; }}
    .btn {{ background: #3182ce; color: #fff; border: none; padding: 10px 20px; font-size: 15px; border-radius: 8px; cursor: pointer; font-weight: 600; transition: background 0.2s; }}
    .btn:hover {{ background: #2b6cb0; }}
    .form-row {{ display: grid; grid-template-columns: 1fr 2fr auto; gap: 12px; margin-bottom: 12px; }}
    input, select {{ padding: 10px 12px; border: 1px solid #e2e8f0; border-radius: 8px; font-size: 14px; }}
    table {{ width: 100%; border-collapse: collapse; margin-top: 12px; font-size: 14px; }}
    th, td {{ padding: 10px 12px; text-align: left; border-bottom: 1px solid #edf2f7; }}
    th {{ background: #edf2f7; font-weight: 600; color: #4a5568; }}
    .tag-part {{ background: #ebf8ff; color: #2b6cb0; padding: 2px 8px; border-radius: 4px; font-weight: 600; }}
    .tag-offset {{ background: #faf5ff; color: #6b46c1; padding: 2px 8px; border-radius: 4px; font-weight: 600; }}
  </style>
</head>
<body>
<div class="container">
  <div class="header">
    <div>
      <h2 style="margin:0 0 4px 0;">Почини.Онлайн &mdash; Сервис заявок (Издатель)</h2>
      <div style="font-size: 14px; color: #718096;">Экземпляр: <b>{INSTANCE_NAME}</b> | Топик: <b>{KAFKA_TOPIC}</b></div>
    </div>
    <div id="status-badge" class="badge {'badge-ok' if is_connected else 'badge-wait'}">
      {'Подключено к Kafka' if is_connected else 'Подключение к Kafka...'}
    </div>
  </div>

  <div class="card">
    <h3 style="margin-top:0;">Оформить новую заявку на ремонт</h3>
    <div class="form-row">
      <input type="text" id="carInput" placeholder="Автомобиль (напр. Kia Rio)">
      <input type="text" id="issueInput" placeholder="Проблема (напр. Замена тормозных колодок)">
      <button class="btn" onclick="sendRequest()">Создать заявку</button>
    </div>
    <div style="font-size: 12px; color: #718096;">Если оставить поля пустыми, данные выберутся случайно для быстрого тестирования.</div>
  </div>

  <div class="card">
    <h3 style="margin-top:0;">Отправленные заявки в Kafka (с подтверждением брокера)</h3>
    <table>
      <thead>
        <tr>
          <th>ID заявки</th>
          <th>Автомобиль</th>
          <th>Неисправность</th>
          <th>Партиция</th>
          <th>Offset</th>
          <th>Время отправки</th>
        </tr>
      </thead>
      <tbody id="requests-tbody">
        <tr><td colspan="6" style="text-align:center; color:#a0aec0;">Заявок пока нет. Нажмите «Создать заявку».</td></tr>
      </tbody>
    </table>
  </div>
</div>

<script>
async function sendRequest() {{
  const car = document.getElementById('carInput').value;
  const issue = document.getElementById('issueInput').value;
  try {{
    const res = await fetch('/api/requests', {{
      method: 'POST',
      headers: {{'Content-Type': 'application/json'}},
      body: JSON.stringify({{car: car || null, issue: issue || null}})
    }});
    if (!res.ok) {{
      const err = await res.json();
      alert('Ошибка: ' + err.detail);
      return;
    }}
    document.getElementById('carInput').value = '';
    document.getElementById('issueInput').value = '';
    loadData();
  }} catch (e) {{
    alert('Сетевая ошибка: ' + e);
  }}
}}

async function loadData() {{
  try {{
    const res = await fetch('/api/requests');
    const data = await res.json();
    const badge = document.getElementById('status-badge');
    if (data.connected) {{
      badge.className = 'badge badge-ok';
      badge.innerText = 'Подключено к Kafka';
    }} else {{
      badge.className = 'badge badge-wait';
      badge.innerText = 'Подключение к Kafka...';
    }}

    const tbody = document.getElementById('requests-tbody');
    if (!data.requests || data.requests.length === 0) {{
      tbody.innerHTML = '<tr><td colspan="6" style="text-align:center; color:#a0aec0;">Заявок пока нет. Нажмите «Создать заявку».</td></tr>';
      return;
    }}

    tbody.innerHTML = data.requests.map(r => `
      <tr>
        <td><b>${{r.request_id}}</b></td>
        <td>${{r.car}}</td>
        <td>${{r.issue}}</td>
        <td><span class="tag-part">P: ${{r.partition}}</span></td>
        <td><span class="tag-offset">#${{r.offset}}</span></td>
        <td style="color:#718096; font-size:13px;">${{r.created_at}}</td>
      </tr>
    `).join('');
  }} catch (e) {{
    console.error('Fetch error:', e);
  }}
}}

loadData();
setInterval(loadData, 2000);
</script>
</body>
</html>"""


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="0.0.0.0", port=PORT, reload=False)

