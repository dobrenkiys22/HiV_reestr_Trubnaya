import os
import json
import base64
import time
import threading

from flask import Flask, request
import requests

app = Flask(__name__)

TELEGRAM_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
APPS_SCRIPT_URL = os.environ["APPS_SCRIPT_URL"]
MANAGER_CHAT_ID = os.environ["MANAGER_CHAT_ID"]
SHEET_NAME = os.environ.get("SHEET_NAME", "июнь 2026")

TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"

# Временное хранилище альбомов (несколько фото одной накладной, отправленных как альбом)
pending_albums = {}
ALBUM_FLUSH_DELAY = 3  # секунд ожидания, прежде чем считать альбом собранным
albums_lock = threading.Lock()


def tg_get_file_path(file_id):
    r = requests.get(f"{TELEGRAM_API}/getFile", params={"file_id": file_id}, timeout=30)
    r.raise_for_status()
    return r.json()["result"]["file_path"]


def tg_download_file(file_path):
    r = requests.get(f"https://api.telegram.org/file/bot{TELEGRAM_TOKEN}/{file_path}", timeout=30)
    r.raise_for_status()
    return r.content


def tg_send_message(chat_id, text):
    try:
        requests.post(
            f"{TELEGRAM_API}/sendMessage",
            json={"chat_id": chat_id, "text": text},
            timeout=15,
        )
    except Exception as e:
        print("Не удалось отправить сообщение в Telegram:", e)


SYSTEM_PROMPT = (
    "Ты распознаёшь фото бумажных приходных накладных ресторана "
    "(УПД / счёт-фактура / товарно-транспортная накладная). "
    "Тебе может быть передано несколько фото одного документа (например, разные страницы "
    "или продолжение таблицы) — анализируй их как единый документ. "
    "Найди: точное название поставщика (юридическое лицо из реквизитов 'Продавец' / "
    "'Грузоотправитель'), дату поставки/отгрузки, итоговую сумму С НДС. "
    "Помимо фото, тебе может быть передана ТЕКСТОВАЯ ПОДПИСЬ к сообщению — это то, что "
    "бармен вручную написал при отправке фото (например, название поставщика и/или дату). "
    "Используй подпись как дополнительный надёжный источник: если на фото название "
    "поставщика нечитаемо, размыто, обрезано или текст слишком мелкий — ориентируйся на "
    "подпись. Если и подпись, и фото читаются, но ЯВНО противоречат друг другу (разные "
    "поставщики или сильно разные даты) — выбери вариант с фото как основной источник "
    "(это первичный документ), но обязательно укажи это противоречие в kommentarii и "
    "поставь uverennost 'low', чтобы менеджер проверил вручную. Если подписи нет вообще — "
    "ориентируйся только на фото, это нормальная ситуация. "
    "ПРАВИЛО ПОИСКА СУММЫ: ищи строку, где написано именно 'Всего к оплате' или "
    "'Итого с НДС' (или 'Стоимость товаров... с налогом — всего' в самой нижней итоговой "
    "строке таблицы) — бери число из ЭТОЙ строки, из колонки 'Стоимость... с налогом — всего' "
    "(а не 'без налога', не 'сумма налога' отдельно, не промежуточные суммы по отдельным товарам). "
    "Если в документе несколько кандидатов на итоговую сумму (например, отдельно по странице 1 "
    "и общий итог по документу) — выбирай САМОЕ БОЛЬШОЕ итоговое число, подписанное как 'Всего "
    "к оплате' / общий итог по документу, а не частичный итог одной страницы. "
    "Если документ на несколько страниц — практически всегда именно последняя страница содержит "
    "финальную строку 'Всего к оплате' со совокупным итогом по всему документу — используй её. "
    "Если поставщик — ООО 'МИЛАРИ', дополнительно определи покупателя по реквизитам покупателя/"
    "грузополучателя: должно быть 'Бегемот' или 'Набойщикова'. "
    "Ответь СТРОГО в виде JSON без markdown, без обратных кавычек и без пояснений, по схеме: "
    '{"postavshik":string,"pokupatel":string или "","date":string в формате ДД.MM.ГГГГ,'
    '"summa":number или null,"kommentarii":string,"uverennost":"high" или "medium" или "low"}. '
    "Поле kommentarii в норме должно быть ПУСТОЙ строкой. Заполняй его только если есть "
    "существенное сомнение в дате, сумме или поставщике — тогда коротко (одна фраза) опиши "
    "именно эту неуверенность. "
    "НЕ перечисляй товарные позиции, суммы НДС по строкам или другие детали таблицы — это не нужно. "
    "Если что-то прочитать не получается уверенно — всё равно дай лучшую разумную догадку, "
    "но поставь uverennost в 'low' или 'medium'."
)


def recognize_invoice(images_base64, caption=""):
    content = [
        {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": img}}
        for img in images_base64
    ]
    user_text = "Распознай эту накладную и верни только JSON."
    if caption:
        user_text += f"\n\nТекстовая подпись бармена к этому сообщению: «{caption}»"
    content.append({"type": "text", "text": user_text})

    last_error = None
    for attempt in range(1, 3):  # одна попытка + один автоповтор
        try:
            resp = requests.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": ANTHROPIC_API_KEY,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": "claude-sonnet-4-6",
                    "max_tokens": 1000,
                    "system": SYSTEM_PROMPT,
                    "messages": [{"role": "user", "content": content}],
                },
                timeout=60,
            )
            print(f"[recognize_invoice] попытка {attempt}: HTTP {resp.status_code}, длина тела ответа: {len(resp.text)}", flush=True)
            if resp.status_code != 200:
                print(f"[recognize_invoice] тело ответа при ошибке: {resp.text[:500]}", flush=True)
            resp.raise_for_status()
            data = resp.json()
            text = "".join(block.get("text", "") for block in data.get("content", []))
            clean = text.replace("```json", "").replace("```", "").strip()
            if not clean:
                raise ValueError(f"Пустой ответ от ИИ (попытка {attempt}). Полный ответ API: {data}")
            return json.loads(clean)
        except Exception as e:
            last_error = e
            print(f"[recognize_invoice] попытка {attempt} не удалась: {repr(e)}", flush=True)
            if attempt == 1:
                time.sleep(2)
                continue
    raise last_error


def write_to_sheet(parsed):
    params = {
        "sheet": SHEET_NAME,
        "supplier": parsed.get("postavshik", ""),
        "date": parsed.get("date", ""),
        "nomer": "",
        "summa": parsed.get("summa") if parsed.get("summa") is not None else "",
        "kommentarii": "",
    }
    if parsed.get("pokupatel"):
        params["pokupatel"] = parsed["pokupatel"]

    r = requests.get(APPS_SCRIPT_URL, params=params, timeout=30)
    r.raise_for_status()
    return r.json()


def process_invoice(images_base64, caption=""):
    print(f"[process_invoice] старт, фото в накладной: {len(images_base64)}, подпись: {caption!r}", flush=True)
    try:
        parsed = recognize_invoice(images_base64, caption)
        print(f"[process_invoice] распознано: {parsed}", flush=True)
    except Exception as e:
        print(f"[process_invoice] ОШИБКА распознавания: {repr(e)}", flush=True)
        tg_send_message(MANAGER_CHAT_ID, f"⚠️ Не удалось распознать накладную.\nОшибка: {e}")
        return

    try:
        result = write_to_sheet(parsed)
        print(f"[process_invoice] результат записи: {result}", flush=True)
    except Exception as e:
        print(f"[process_invoice] ОШИБКА записи в таблицу: {repr(e)}", flush=True)
        tg_send_message(
            MANAGER_CHAT_ID,
            f"⚠️ Накладная распознана, но НЕ записалась в таблицу.\n"
            f"Ошибка: {e}\nРаспознанные данные: {parsed}",
        )
        return

    if result.get("status") == "ok":
        warn = ""
        if parsed.get("uverennost") in ("low", "medium"):
            warn = "\n⚠️ Не 100% уверенность в распознавании — пожалуйста, проверь эту строку."
        if parsed.get("kommentarii"):
            warn += f"\n📝 {parsed.get('kommentarii')}"
        pokup = f" ({parsed.get('pokupatel')})" if parsed.get("pokupatel") else ""
        tg_send_message(
            MANAGER_CHAT_ID,
            f"✅ Записано: {parsed.get('postavshik')}{pokup}\n"
            f"Дата: {parsed.get('date')} | Сумма: {parsed.get('summa')}"
            f"{warn}\n"
            f"Строка {result.get('row')} в листе «{result.get('sheet')}»",
        )
    else:
        tg_send_message(
            MANAGER_CHAT_ID,
            f"❌ Ошибка записи в таблицу: {result.get('message')}\n"
            f"Распознанные данные: {parsed}\n"
            f"Нужно занести эту накладную вручную.",
        )


def album_flusher():
    """Фоновый поток: следит за накопленными альбомами фото и обрабатывает их,
    когда новых фото в альбом не поступало некоторое время."""
    print("[album_flusher] фоновый поток запущен", flush=True)
    while True:
        try:
            time.sleep(1)
            now = time.time()
            to_process = []
            with albums_lock:
                for gid in list(pending_albums.keys()):
                    entry = pending_albums[gid]
                    if now - entry["ts"] > ALBUM_FLUSH_DELAY:
                        to_process.append({"images": entry["images"], "caption": entry["caption"]})
                        del pending_albums[gid]
            for entry_data in to_process:
                print(f"[album_flusher] обрабатываю альбом, фото: {len(entry_data['images'])}, подпись: {entry_data['caption']!r}", flush=True)
                process_invoice(entry_data["images"], entry_data["caption"])
        except Exception as e:
            print(f"[album_flusher] ОШИБКА в фоновом потоке: {repr(e)}", flush=True)


@app.route("/webhook", methods=["POST"])
def webhook():
    ensure_flusher_started()
    update = request.get_json(silent=True) or {}
    print(f"[webhook] получено обновление: {update}", flush=True)
    msg = update.get("message")

    if not msg:
        print("[webhook] в обновлении нет message, пропускаю", flush=True)
        return "ok"

    if "text" in msg and msg["text"].strip() == "/start":
        tg_send_message(
            msg["chat"]["id"],
            "Привет! Присылай сюда фото накладных — я распознаю и сам занесу в реестр. "
            "Если накладная на несколько страниц — отправляй фото как альбом (выбери все сразу).",
        )
        return "ok"

    if "photo" not in msg:
        print("[webhook] в message нет photo, пропускаю", flush=True)
        return "ok"

    file_id = msg["photo"][-1]["file_id"]  # самое крупное по размеру фото
    media_group_id = msg.get("media_group_id")
    caption = (msg.get("caption") or "").strip()
    print(f"[webhook] фото получено, media_group_id={media_group_id}, caption={caption!r}", flush=True)

    try:
        file_path = tg_get_file_path(file_id)
        img_bytes = tg_download_file(file_path)
        img_b64 = base64.b64encode(img_bytes).decode()
        print(f"[webhook] фото скачано, размер base64: {len(img_b64)}", flush=True)
    except Exception as e:
        print(f"[webhook] ОШИБКА скачивания фото: {repr(e)}", flush=True)
        tg_send_message(MANAGER_CHAT_ID, f"⚠️ Не удалось загрузить фото из Telegram: {e}")
        return "ok"

    if media_group_id:
        with albums_lock:
            entry = pending_albums.setdefault(media_group_id, {"images": [], "caption": "", "ts": time.time()})
            entry["images"].append(img_b64)
            if caption:
                entry["caption"] = caption
            entry["ts"] = time.time()
        print(f"[webhook] фото добавлено в альбом {media_group_id}, всего в альбоме: {len(entry['images'])}", flush=True)
    else:
        print("[webhook] одиночное фото, обрабатываю сразу (синхронно)", flush=True)
        process_invoice([img_b64], caption)

    return "ok"


@app.route("/", methods=["GET"])
def health():
    return "Бот работает"


_flusher_started = False
_flusher_lock = threading.Lock()


def ensure_flusher_started():
    global _flusher_started
    if _flusher_started:
        return
    with _flusher_lock:
        if not _flusher_started:
            threading.Thread(target=album_flusher, daemon=True).start()
            _flusher_started = True
            print("[ensure_flusher_started] фоновый поток запущен внутри воркера", flush=True)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
