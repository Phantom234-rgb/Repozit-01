"""Однофайловый Telegram Business-бот. Инструкция запуска — в конце файла."""

import asyncio
import copy
import json
import logging
import os
import re
from pathlib import Path

from aiogram import Bot, Dispatcher, F, Router
from aiogram.exceptions import TelegramAPIError, TelegramRetryAfter
from aiogram.filters import Command, CommandObject
from aiogram.types import BusinessConnection, ErrorEvent, Message


log = logging.getLogger("business_mute")
USERNAME = re.compile(r"@[a-zA-Z][a-zA-Z0-9_]{0,31}\Z")
HELP = (
    "/mute <user_id или @username> — удалять новые входящие сообщения\n"
    "/unmute <user_id или @username> — отключить удаление\n"
    "/list — список правил\n"
    "Неизвестный @username привязывается к ID при первом входящем сообщении.\n"
    "После смены username используйте /unmute с ID из /list."
)


class MuteStore:
    """Все изменения сериализуются; повреждённый JSON не затирается молча."""

    def __init__(self, path: Path, owner_id: int):
        self.path = path
        self.lock = asyncio.Lock()
        self.data = {"owner_id": owner_id, "muted": {}, "pending": []}
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            if (
                not isinstance(data, dict)
                or data.get("owner_id") != owner_id
                or not isinstance(data.get("muted"), dict)
                or not isinstance(data.get("pending"), list)
            ):
                raise ValueError("Неверный формат mute-файла или другой OWNER_ID")
            for key, alias in data["muted"].items():
                if not key.isascii() or not key.isdigit() or int(key) <= 0:
                    raise ValueError("Неверный user_id в mute-файле")
                if alias is not None and not USERNAME.fullmatch(alias):
                    raise ValueError("Неверный username в mute-файле")
            if not all(isinstance(x, str) and USERNAME.fullmatch(x) for x in data["pending"]):
                raise ValueError("Неверный список ожидающих username")
            self.data = data

    def _write(self, data: dict):
        # Временный файл на том же диске + атомарная замена готового JSON.
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        with temporary.open("w", encoding="utf-8") as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, self.path)

    async def change(self, action):
        async with self.lock:
            candidate = copy.deepcopy(self.data)
            result = action(candidate)
            if candidate != self.data:
                # Публикуем изменения в памяти только после успешной записи.
                await asyncio.to_thread(self._write, candidate)
                self.data = candidate
            return result

    async def observe(self, user_id: int, username: str | None) -> bool:
        key = str(user_id)
        alias = "@" + username.lower() if username else None

        def action(data):
            if alias and alias in data["pending"]:
                data["pending"].remove(alias)
                data["muted"][key] = alias
                log.info("Правило %s привязано к user_id=%s", alias, user_id)
            if key in data["muted"]:
                data["muted"][key] = alias
                return True
            return False

        return await self.change(action)

    async def set_mute(self, target: str, enabled: bool) -> str:
        def action(data):
            muted, pending = data["muted"], data["pending"]
            if target.startswith("@"):
                ids = [key for key, alias in muted.items() if alias == target]
                if enabled:
                    if not ids and target not in pending:
                        pending.append(target)
                    return f"{target}: удаление включено; привязка к ID — при входящем сообщении."
                for key in ids:
                    del muted[key]
                if target in pending:
                    pending.remove(target)
                return f"{target}: удаление выключено."
            if enabled:
                muted.setdefault(target, None)
            else:
                old_alias = muted.pop(target, None)
                if old_alias in pending:
                    pending.remove(old_alias)
            return f"ID {target}: удаление {'включено' if enabled else 'выключено'}."

        return await self.change(action)

    async def lines(self) -> list[str]:
        async with self.lock:
            lines = [f"{key} {alias or ''}".strip() for key, alias in self.data["muted"].items()]
            lines += [f"{alias} — ожидает входящего сообщения" for alias in self.data["pending"]]
            return lines


def create_dispatcher(owner_id: int, store: MuteStore) -> Dispatcher:
    dp = Dispatcher()
    router = Router()
    # Ключ — именно business_connection_id, а не chat_id.
    connections: dict[str, BusinessConnection] = {}

    async def remember(connection: BusinessConnection, bot: Bot):
        if connection.user.id != owner_id:
            log.warning("Игнорируется чужое business-подключение: owner=%s", connection.user.id)
            return
        connections[connection.id] = connection
        can_delete = bool(connection.rights and connection.rights.can_delete_all_messages)
        log.info("Business connection=%s enabled=%s can_delete_all_messages=%s",
                 connection.id, connection.is_enabled, can_delete)
        if not connection.is_enabled:
            text = "Business-подключение отключено. Удаление сообщений остановлено."
        elif not can_delete:
            text = (
                "Нет права can_delete_all_messages. В настройках Business-бота разрешите "
                "удаление всех сообщений. Права can_delete_sent_messages "
                "(ранее can_delete_outgoing_messages) для входящих недостаточно."
            )
        else:
            text = "Business-бот подключён. Право удаления входящих сообщений есть."
        try:
            # Это обычное сообщение от бота владельцу, без business_connection_id.
            await bot.send_message(connection.user_chat_id, text)
        except TelegramAPIError as exc:
            log.error("Не удалось уведомить владельца: %s", exc)

    @router.business_connection()
    async def on_connection(connection: BusinessConnection, bot: Bot):
        await remember(connection, bot)

    @router.business_message()
    async def on_business_message(message: Message, bot: Bot):
        connection_id = message.business_connection_id
        if not connection_id or message.chat.type != "private":
            return
        connection = connections.get(connection_id)
        if connection is None:
            # После перезапуска business_connection может повторно не прийти.
            try:
                connection = await bot.get_business_connection(connection_id)
                await remember(connection, bot)
            except TelegramAPIError as exc:
                log.error("Не удалось получить connection=%s: %s", connection_id, exc)
                return
        if connection.user.id != owner_id or not connection.is_enabled:
            return
        sender = message.from_user
        # Исходящие владельца и других business-ботов никогда не удаляем.
        if sender is None or sender.id == owner_id or message.sender_business_bot:
            return
        if sender.id != message.chat.id:
            return
        try:
            muted = await store.observe(message.chat.id, sender.username)
        except OSError:
            log.exception("Ошибка сохранения правила для user_id=%s", sender.id)
            # Уже сохранённое правило продолжает работать при ошибке диска.
            muted = str(sender.id) in store.data["muted"] or (
                sender.username is not None
                and "@" + sender.username.lower() in store.data["pending"]
            )
        if not muted:
            return
        # Проверка прав делается при подключении; сам API остаётся источником истины.
        # При 429 повторяем запрос один раз через указанный Telegram интервал.
        for attempt in range(2):
            try:
                await bot.delete_business_messages(
                    business_connection_id=connection.id,
                    message_ids=[message.message_id],
                )
                log.info("Удалено: user_id=%s chat_id=%s message_id=%s connection=%s",
                         sender.id, message.chat.id, message.message_id, connection.id)
                return
            except TelegramRetryAfter as exc:
                log.warning("Лимит удаления: chat_id=%s message_id=%s retry_after=%s",
                            message.chat.id, message.message_id, exc.retry_after)
                if attempt == 0:
                    await asyncio.sleep(exc.retry_after)
            except TelegramAPIError as exc:
                log.error("Удаление не выполнено: chat_id=%s message_id=%s connection=%s: %s",
                          message.chat.id, message.message_id, connection.id, exc)
                return

    # Команды регистрируем только на message, не на business_message.
    router.message.filter(F.chat.type == "private", F.from_user.id == owner_id,
                          F.business_connection_id == None)  # noqa: E711

    @router.message(Command("start", "help"))
    async def start(message: Message):
        status = "\nBusiness-подключение ещё не получено. Подключите бота в настройках Telegram."
        if connections:
            active = [c for c in connections.values() if c.is_enabled]
            if not active:
                status = "\nBusiness-подключение отключено."
            elif any(c.rights and c.rights.can_delete_all_messages for c in active):
                status = "\nBusiness-подключение активно; право удаления входящих есть."
            else:
                status = "\nВНИМАНИЕ: нет can_delete_all_messages. Разрешите удаление всех сообщений."
        await message.answer(HELP + status)

    @router.message(Command("mute", "unmute"))
    async def change_mute(message: Message, command: CommandObject):
        target = (command.args or "").strip()
        if target.isascii() and target.isdigit() and 0 < int(target) < 2**52:
            target = str(int(target))
            if int(target) == owner_id:
                await message.answer("Свой аккаунт добавлять в список не нужно.")
                return
        elif USERNAME.fullmatch(target):
            target = target.lower()
        else:
            await message.answer(f"Формат: /{command.command} <положительный user_id или @username>")
            return
        enabled = command.command.lower() == "mute"
        try:
            result = await store.set_mute(target, enabled)
        except OSError:
            log.exception("Не удалось сохранить изменение mute-списка")
            await message.answer("Ошибка записи JSON. Изменение не применено; проверьте лог.")
            return
        log.info("owner=%s command=%s target=%s", owner_id, command.command, target)
        await message.answer(result)

    @router.message(Command("list"))
    async def list_mutes(message: Message):
        lines = await store.lines()
        if not lines:
            await message.answer("Mute-список пуст.")
            return
        # Не превышаем ограничение Telegram на длину одного сообщения.
        chunk = "Mute-список:\n"
        for line in lines:
            if len(chunk) + len(line) + 1 > 3800:
                await message.answer(chunk)
                chunk = ""
            chunk += line + "\n"
        if chunk:
            await message.answer(chunk)

    @dp.errors()
    async def on_error(event: ErrorEvent):
        log.error("Ошибка обработки update_id=%s", event.update.update_id,
                  exc_info=(type(event.exception), event.exception, event.exception.__traceback__))
        return True

    dp.include_router(router)
    return dp


async def main():
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    token ='9106037755:AAGZhuWnpLQ_bi9PjNyaa1VOGCQn-byQcGM'
    owner ='5164269905'
    if not token or not owner.isascii() or not owner.isdigit() or int(owner) <= 0:
        raise SystemExit("Задайте BOT_TOKEN и положительный числовой OWNER_ID в окружении.")
    path = Path(__file__).resolve().with_name("mute_list.json")
    store = MuteStore(path, int(owner))
    dp = create_dispatcher(int(owner), store)
    async with Bot(token=token) as bot:
        git push
        if not me.can_connect_to_business:
            raise SystemExit("Включите Business Mode у бота в @BotFather.")
        log.info("Запуск @%s; owner=%s; JSON=%s", me.username, owner, path)
        await bot.delete_webhook(drop_pending_updates=False)
        await dp.start_polling(bot, allowed_updates=["message", "business_connection", "business_message"])


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass


# ИНСТРУКЦИЯ ЗАПУСКА
# 1. Python 3.10+. Создайте бота через /newbot в @BotFather и получите токен.
# 2. @BotFather -> /mybots -> ваш бот -> Bot Settings -> Business Mode -> Enable.
#    В новых версиях интерфейса этот режим может называться Secretary Mode.
# 3. В PowerShell из папки файла:
#       python -m venv .venv
#       .\.venv\Scripts\python.exe -m pip install "aiogram>=3.20,<4"
#       $env:BOT_TOKEN = "ТОКЕН_ОТ_BOTFATHER"
#       $env:OWNER_ID = "123456789"
#       .\.venv\Scripts\python.exe business_mute_bot.py
#    OWNER_ID — ваш числовой Telegram ID, не номер телефона и не username.
#    Зависимость только aiogram; файловые операции — asyncio.to_thread из Python.
#    aiofiles не нужен. aiogram 3.20 поддерживает Bot API 9.0; можно ставить новее.
#    Используется облачный api.telegram.org; свой Bot API сервер должен быть >=9.0.
# 4. Напишите боту /start со своего аккаунта, чтобы получать предупреждения.
# 5. Telegram -> Settings -> Telegram Business -> Chatbots (или Bots) -> ваш бот.
#    Аккаунту должен быть доступен Telegram Business. Включите управление нужными
#    личными чатами: существующими и новыми, контактами и не контактами по необходимости.
#    Исключённые чаты бот не получает. Дайте право удаления ВСЕХ сообщений
#    (can_delete_all_messages). Изменять права программно бот не может.
# 6. В обычном личном чате с ботом: /mute 123456789 или /mute @username;
#    /list; /unmute 123456789 или /unmute @username. Команды принимает только OWNER_ID.
#    @username — правило до первого входящего, затем устойчивое правило по ID.
#    Уже привязанный ID остаётся замьюченным при смене username; снимайте по ID.
#    Пока username не привязан, правило относится к текущему владельцу этого имени.
#    Произвольного человека по username Bot API искать не умеет.
# 7. Список хранится рядом с этим файлом в mute_list.json; запускайте один экземпляр.
#    Подключения хранятся в памяти и восстанавливаются через getBusinessConnection
#    по business_connection_id следующего business_message после перезапуска.
#    Бот удаляет новые входящие, не выгружает и не чистит историю переписки.
#    Мгновенность зависит от доставки апдейта, сети и лимитов Telegram.
#    Удаление подчиняется ограничениям deleteMessage (в том числе возрасту сообщения).
#    Остановленный бот не удаляет в реальном времени; Telegram хранит апдейты до 24 ч.
#    /unmute останавливает будущие удаления, но не восстанавливает удалённые сообщения.
#    Блокировка собеседников и отметка прочтения здесь не используются.
