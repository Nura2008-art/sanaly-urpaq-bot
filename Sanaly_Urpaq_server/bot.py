import hmac
import json
import os
import re
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


class TelegramError(Exception):
    pass


class Telegram:
    def __init__(self, token):
        self.base = f"https://api.telegram.org/bot{token}/"

    def call(self, method, **data):
        request = Request(
            self.base + method,
            data=json.dumps(data).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        try:
            with urlopen(request, timeout=40) as response:
                result = json.load(response)
        except HTTPError as error:
            raise TelegramError(f"HTTP {error.code}") from None
        except (URLError, TimeoutError, ValueError):
            raise TelegramError("Ошибка соединения или ответа Telegram") from None
        if not result.get("ok"):
            raise TelegramError(f"API {result.get('error_code', 'error')}")
        return result["result"]


LANGUAGES = {
    "Русский": {
        "topics": ["Буллинг", "Насилие", "Другое"],
        "pick": "Выбери тему вымышленного обращения.",
        "describe": "Опиши вымышленную ситуацию до 500 символов. Используй придуманные данные.",
        "review": "Проверь текст. Отправить?",
        "confirm": "Подтвердить",
        "cancel": "Отмена",
        "done": "Тестовое обращение доставлено.",
        "failed": "Доставка не удалась. Проверьте настройки группы и повтори подтверждение.",
    },
    "Қазақша": {
        "topics": ["Буллинг", "Зорлық-зомбылық", "Басқа"],
        "pick": "Сынақ хабарламасының тақырыбын таңда.",
        "describe": "500 таңбаға дейін ойдан шығарылған оқиғаны жаз.",
        "review": "Мәтінді тексер. Жіберейік пе?",
        "confirm": "Растау",
        "cancel": "Бас тарту",
        "done": "Сынақ хабарламасы жеткізілді.",
        "failed": "Хабарлама жеткізілмеді. Топ баптауларын тексеріп, қайта раста.",
    },
}
CHOOSE = "Выбери язык / Тілді таңда."


class Bot:
    def __init__(self, api, state_path, owner_code, tester_code):
        self.api = api
        self.path = state_path
        self.owner_code = owner_code
        self.tester_code = tester_code
        self.state = json.loads(self.path.read_text()) if self.path.exists() else {}
        self.sessions = {}
        self.username = self.api.call("getMe")["username"]

    def save(self, **changes):
        state = self.state | changes
        temp = self.path.with_suffix(".tmp")
        temp.write_text(json.dumps(state), encoding="utf-8")
        temp.replace(self.path)
        self.state = state

    def send(self, chat, text, buttons=()):
        data = {"chat_id": chat, "text": text}
        if buttons:
            data["reply_markup"] = {
                "keyboard": [[{"text": button} for button in buttons]],
                "resize_keyboard": True,
            }
        self.api.call("sendMessage", **data)

    def handle(self, update):
        msg = update.get("message", {})
        text = msg.get("text", "").strip()
        if not text:
            return
        chat = msg["chat"]["id"]
        kind = msg["chat"]["type"]
        owner = self.state.get("owner")

        if kind in ("group", "supergroup"):
            command = f"/bindgroup@{self.username}"
            if (text.lower() == command.lower() and owner is not None
                    and msg.get("from", {}).get("id") == owner and not msg.get("sender_chat")):
                info = self.api.call("getChat", chat_id=chat)
                if info.get("username") or info.get("active_usernames"):
                    self.send(chat, "Выбери закрытую группу для тестирования.")
                    return
                self.send(chat, "Тестовая группа подключена.")
                self.save(group=chat)
            return
        if kind != "private":
            return

        if owner is None and hmac.compare_digest(text.encode(), f"/start {self.owner_code}".encode()):
            self.save(owner=chat)
            owner = chat
            text = "/start"
        if (self.state.get("group") and self.state.get("tester") is None and chat != owner
                and hmac.compare_digest(text.encode(), f"/start {self.tester_code}".encode())):
            self.save(tester=chat)
            text = "/start"
        if chat not in (owner, self.state.get("tester")):
            if text.startswith("/start"):
                self.send(chat, "Закрытое тестирование / Жабық сынақ.")
            return

        if text in ("/start", "Отмена", "Бас тарту"):
            self.sessions.pop(chat, None)
        session = self.sessions.setdefault(chat, {})
        ui = session.get("ui")
        if ui is None:
            if text in LANGUAGES:
                ui = LANGUAGES[text]
                self.send(chat, ui["pick"], [*ui["topics"], ui["cancel"]])
                session["ui"] = ui
            else:
                self.send(chat, CHOOSE, LANGUAGES)
            return
        if "topic" not in session:
            if text in ui["topics"]:
                self.send(chat, ui["describe"], [ui["cancel"]])
                session["topic"] = text
            else:
                self.send(chat, ui["pick"], [*ui["topics"], ui["cancel"]])
            return
        if "description" not in session:
            if len(text) <= 500 and not text.startswith("/"):
                self.send(chat, f'{ui["review"]}\n\n{session["topic"]}\n{text}', [ui["confirm"], ui["cancel"]])
                session["description"] = text
            else:
                self.send(chat, ui["describe"], [ui["cancel"]])
            return
        if text != ui["confirm"]:
            self.send(chat, ui["review"], [ui["confirm"], ui["cancel"]])
            return
        group = self.state.get("group")
        number = update["update_id"]
        try:
            if group is None:
                raise TelegramError("Группа не подключена")
            info = self.api.call("getChat", chat_id=group)
            if info.get("username") or info.get("active_usernames"):
                raise TelegramError("Группа открыта")
            self.send(group, f'[ТЕСТ / СЫНАҚ] №{number}\nҚазығұрт ауданы\n{session["topic"]}\n{session["description"]}')
        except TelegramError:
            self.send(chat, ui["failed"], [ui["confirm"], ui["cancel"]])
            return
        self.sessions.pop(chat, None)
        self.send(chat, f'{ui["done"]} №{number}', LANGUAGES)

    def run(self):
        print(f"@{self.username}: бот запущен, тестовый режим.", flush=True)
        offset = self.state.get("offset", 0)
        while True:
            try:
                updates = self.api.call("getUpdates", offset=offset, timeout=25, allowed_updates=["message"])
            except TelegramError as error:
                print(f"Опрос Telegram: {error}", flush=True)
                time.sleep(8)
                continue
            for update in updates:
                offset = update["update_id"] + 1
                self.save(offset=offset)
                try:
                    self.handle(update)
                except TelegramError as error:
                    print(f"Обработка сообщения: {error}", flush=True)


def main():
    token = os.environ.get("BOT_TOKEN", "").strip()
    owner_code = os.environ.get("OWNER_CODE", "").strip()
    tester_code = os.environ.get("TESTER_CODE", "").strip()
    if not token or owner_code == tester_code or not all(
        re.fullmatch(r"[A-Za-z0-9_-]{20,64}", code) for code in (owner_code, tester_code)
    ):
        raise SystemExit("Задай BOT_TOKEN и два разных кода OWNER_CODE и TESTER_CODE (20–64 символа).")
    folder = Path(os.environ.get("RAILWAY_VOLUME_MOUNT_PATH", "/data"))
    folder.mkdir(parents=True, exist_ok=True)
    try:
        Bot(Telegram(token), folder / "state.json", owner_code, tester_code).run()
    except TelegramError as error:
        raise SystemExit(f"Запуск: {error}") from None


if __name__ == "__main__":
    main()
