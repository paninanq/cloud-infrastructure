from __future__ import annotations

import asyncio
from collections import deque
from contextlib import asynccontextmanager
from datetime import datetime
import json
import logging
import os

from aiokafka import AIOKafkaConsumer
from fastapi import FastAPI
from fastapi.responses import HTMLResponse

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("dispatcher-consumer")

KAFKA_BOOTSTRAP = os.getenv("KAFKA_BOOTSTRAP_SERVERS", "localhost:9092")
KAFKA_TOPIC = os.getenv("KAFKA_TOPIC", "repair-requests")
KAFKA_GROUP_ID = os.getenv("KAFKA_GROUP_ID", "dispatcher-group")
INSTANCE_NAME = os.getenv("INSTANCE_NAME", "dispatcher-1")
PORT = int(os.getenv("PORT", "8002"))
PROCESSING_DELAY = float(os.getenv("PROCESSING_DELAY", "0.8"))

consumer: AIOKafkaConsumer | None = None
is_connected = False
consumer_task: asyncio.Task | None = None
recent_processed: deque[dict] = deque(maxlen=50)


async def consume_loop():
    global consumer, is_connected
    while True:
        try:
            logger.info("[%s] Connecting to Kafka %s, group=%s, topic=%s...", INSTANCE_NAME, KAFKA_BOOTSTRAP, KAFKA_GROUP_ID, KAFKA_TOPIC)
            c = AIOKafkaConsumer(
                KAFKA_TOPIC,
                bootstrap_servers=KAFKA_BOOTSTRAP,
                group_id=KAFKA_GROUP_ID,
                auto_offset_reset="earliest",
                enable_auto_commit=True,
            )
            await c.start()
            consumer = c
            is_connected = True
            logger.info("[%s] Consumer joined group %s and started reading %s", INSTANCE_NAME, KAFKA_GROUP_ID, KAFKA_TOPIC)
            break
        except Exception as e:
            logger.warning("[%s] Kafka connection error (%s). Retrying in 2 seconds...", INSTANCE_NAME, e)
            await asyncio.sleep(2)

    try:
        async for msg in consumer:
            try:
                data = json.loads(msg.value.decode("utf-8"))
            except Exception:
                data = {"raw": msg.value.decode("utf-8", errors="replace")}

            logger.info(
                "[%s] Processing: topic=%s partition=%s offset=%s key=%s data=%s",
                INSTANCE_NAME, msg.topic, msg.partition, msg.offset, msg.key, data,
            )

            # Имитируем работу диспетчера (назначение автосервиса, расчёт времени)
            if PROCESSING_DELAY > 0:
                await asyncio.sleep(PROCESSING_DELAY)

            item = {
                "request_id": data.get("request_id", "N/A"),
                "car": data.get("car", "N/A"),
                "issue": data.get("issue", "N/A"),
                "partition": msg.partition,
                "offset": msg.offset,
                "handler_instance": INSTANCE_NAME,
                "processed_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            }
            recent_processed.appendleft(item)
    except asyncio.CancelledError:
        logger.info("[%s] Consumer task cancelled", INSTANCE_NAME)
    except Exception as e:
        logger.error("[%s] Consumer loop encountered error: %s", INSTANCE_NAME, e)
        is_connected = False


@asynccontextmanager
async def lifespan(app: FastAPI):
    global consumer_task
    consumer_task = asyncio.create_task(consume_loop())
    yield
    if consumer_task:
        consumer_task.cancel()
    if consumer:
        logger.info("[%s] Closing consumer...", INSTANCE_NAME)
        await consumer.stop()


app = FastAPI(title="Почини.Онлайн - Сервис обработки заявок (Consumer)", lifespan=lifespan)


@app.get("/api/processed")
async def list_processed():
    return {
        "connected": is_connected,
        "instance": INSTANCE_NAME,
        "group_id": KAFKA_GROUP_ID,
        "topic": KAFKA_TOPIC,
        "items": list(recent_processed),
    }


@app.get("/", response_class=HTMLResponse)
async def index():
    return f"""<!DOCTYPE html>
<html lang="ru">
<head>
  <meta charset="UTF-8">
  <title>Почини.Онлайн — Диспетчер заявок (Обработчик)</title>
  <style>
    body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; margin: 0; padding: 24px; background: #f5f7fa; color: #2d3748; }}
    .container {{ max-width: 900px; margin: 0 auto; }}
    .header {{ background: #fff; padding: 20px 24px; border-radius: 12px; box-shadow: 0 2px 4px rgba(0,0,0,0.06); margin-bottom: 20px; display: flex; justify-content: space-between; align-items: center; }}
    .badge {{ display: inline-block; padding: 4px 10px; border-radius: 6px; font-size: 13px; font-weight: 600; }}
    .badge-ok {{ background: #c6f6d5; color: #22543d; }}
    .badge-wait {{ background: #feebc8; color: #744210; }}
    .card {{ background: #fff; padding: 24px; border-radius: 12px; box-shadow: 0 2px 4px rgba(0,0,0,0.06); margin-bottom: 20px; }}
    .meta-box {{ display: flex; gap: 20px; font-size: 14px; color: #4a5568; background: #edf2f7; padding: 12px 16px; border-radius: 8px; margin-bottom: 16px; }}
    table {{ width: 100%; border-collapse: collapse; margin-top: 12px; font-size: 14px; }}
    th, td {{ padding: 10px 12px; text-align: left; border-bottom: 1px solid #edf2f7; }}
    th {{ background: #edf2f7; font-weight: 600; color: #4a5568; }}
    .tag-part {{ background: #ebf8ff; color: #2b6cb0; padding: 2px 8px; border-radius: 4px; font-weight: 600; }}
    .tag-offset {{ background: #faf5ff; color: #6b46c1; padding: 2px 8px; border-radius: 4px; font-weight: 600; }}
    .tag-instance {{ background: #feebc8; color: #975a16; padding: 2px 8px; border-radius: 4px; font-weight: 600; }}
  </style>
</head>
<body>
<div class="container">
  <div class="header">
    <div>
      <h2 style="margin:0 0 4px 0;">Почини.Онлайн &mdash; Диспетчер заявок (Обработчик)</h2>
      <div style="font-size: 14px; color: #718096;">Экземпляр: <b>{INSTANCE_NAME}</b></div>
    </div>
    <div id="status-badge" class="badge {'badge-ok' if is_connected else 'badge-wait'}">
      {'Подключено к Kafka' if is_connected else 'Подключение к Kafka...'}
    </div>
  </div>

  <div class="card">
    <div class="meta-box">
      <div>Группа потребителей: <b>{KAFKA_GROUP_ID}</b></div>
      <div>Топик: <b>{KAFKA_TOPIC}</b></div>
      <div>Экземпляр: <b>{INSTANCE_NAME}</b></div>
    </div>
    <h3 style="margin-top:0;">Обработанные заявки в реальном времени</h3>
    <table>
      <thead>
        <tr>
          <th>ID заявки</th>
          <th>Автомобиль</th>
          <th>Неисправность</th>
          <th>Партиция</th>
          <th>Offset</th>
          <th>Обработчик</th>
          <th>Время обработки</th>
        </tr>
      </thead>
      <tbody id="processed-tbody">
        <tr><td colspan="7" style="text-align:center; color:#a0aec0;">Ожидание сообщений из топика...</td></tr>
      </tbody>
    </table>
  </div>
</div>

<script>
async function loadData() {{
  try {{
    const res = await fetch('/api/processed');
    const data = await res.json();
    const badge = document.getElementById('status-badge');
    if (data.connected) {{
      badge.className = 'badge badge-ok';
      badge.innerText = 'Подключено к Kafka';
    }} else {{
      badge.className = 'badge badge-wait';
      badge.innerText = 'Подключение к Kafka...';
    }}

    const tbody = document.getElementById('processed-tbody');
    if (!data.items || data.items.length === 0) {{
      tbody.innerHTML = '<tr><td colspan="7" style="text-align:center; color:#a0aec0;">Ожидание сообщений из топика...</td></tr>';
      return;
    }}

    tbody.innerHTML = data.items.map(item => `
      <tr>
        <td><b>${{item.request_id}}</b></td>
        <td>${{item.car}}</td>
        <td>${{item.issue}}</td>
        <td><span class="tag-part">P: ${{item.partition}}</span></td>
        <td><span class="tag-offset">#${{item.offset}}</span></td>
        <td><span class="tag-instance">${{item.handler_instance}}</span></td>
        <td style="color:#718096; font-size:13px;">${{item.processed_at}}</td>
      </tr>
    `).join('');
  }} catch (e) {{
    console.error('Fetch error:', e);
  }}
}}

loadData();
setInterval(loadData, 1500);
</script>
</body>
</html>"""


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="0.0.0.0", port=PORT, reload=False)

