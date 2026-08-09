# PROJECT M backend v1

Первый backend-каркас для PROJECT M ↔ QTickets.

## Уже работает в коде

- один endpoint: `POST /api/qtickets/webhook`
- проверка `X-Signature` через HMAC-SHA1
- мероприятие `251223`
- обработка:
  - `Заказ оплачен`
  - `Заказ отменен`
  - `Заказ возвращен`
  - `Сканирование на вход`
  - `Сканирование на выход`
- база заказов и отдельных билетов
- barcode каждого билета
- статусы `active / used / refunded / cancelled`
- проверка возраста без хранения паспортных данных
- `GET /api/tickets/{barcode}`
- `POST /api/tickets/{barcode}/verify-age`

## Настройка QTickets

После деплоя backend будет URL вида:

`https://YOUR-DOMAIN/api/qtickets/webhook`

Создаём несколько webhook-записей, но у всех указываем ОДИН URL и ОДИН секрет:

1. Заказ оплачен
2. Заказ отменен
3. Заказ возвращен
4. Сканирование на вход
5. Сканирование на выход

`Новый заказ` и `Заказ изменен` пока можно не включать.

QTickets передаёт тип события в `X-Event-Type`, поэтому отдельные URL не нужны.

## Запуск

1. Создать PostgreSQL.
2. Выполнить `schema.sql`.
3. Скопировать `.env.example` в `.env`.
4. Заполнить `DATABASE_URL` и `QTICKETS_WEBHOOK_SECRET`.
5. `pip install -r requirements.txt`
6. `uvicorn app.main:app --host 0.0.0.0 --port 8000`

## Следующий этап

- деплой;
- подключение реального webhook;
- тестовая покупка;
- определение тарифа «Быстрый вход» / «Стандарт» из реального payload;
- связь заказа с Telegram-пользователем;
- подключение Mini App.
