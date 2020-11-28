import datetime
import enum
import os
import time
from contextlib import contextmanager
from threading import Thread
from typing import List, Optional

import geocoder
import pytz
import telebot
from loguru import logger
from pydantic import BaseModel
from timezonefinder import TimezoneFinder
from tinydb import Query
from tinydb import TinyDB
from tinydb.storages import MemoryStorage
from transitions import Machine, MachineError

token = os.getenv("TELEGRAM_BOT_TOKEN")

bot = telebot.TeleBot(token)

timezone_finder = TimezoneFinder()

db_path = os.getenv("DB_PATH")
if db_path:
    logger.info(f'init db on path={db_path}')
    db = TinyDB(db_path)
else:
    logger.info('init in-memory db')
    db = TinyDB(storage=MemoryStorage)


class State(str, enum.Enum):
    INITIAL = "initial"
    HISTORY = "history"
    START = "start"
    NOTIFY_MORNING = "notify_morning"
    NOTIFY_EVENING = "notify_evening"
    NOTIFY_FORGOT = "notify_forgot"
    WAIT_FOR_MORNING_TIME = "wait_for_morning_time"
    WAIT_FOR_EVENING_TIME = "wait_for_evening_time"
    WAIT_FOR_TZ = "wait_for_tz"
    WAIT = "wait"
    RECORD = "record"
    STOP = "stop"
    STATUS = "status"


transitions = [
    ["to_start", State.INITIAL, State.START],
    ["to_wait", '*', State.WAIT],
    ["to_wait_for_tz", [State.STOP, State.WAIT], State.WAIT_FOR_TZ],
    ["to_wait_for_morning_time", [State.STOP, State.WAIT, State.WAIT_FOR_TZ], State.WAIT_FOR_MORNING_TIME],
    ["to_wait_for_evening_time", State.WAIT_FOR_MORNING_TIME, State.WAIT_FOR_EVENING_TIME],
    ["to_record", State.WAIT, State.RECORD],
    ["to_stop", '*', State.STOP],
    ["to_history", State.WAIT, State.HISTORY],
    ["to_notify_morning", State.WAIT, State.NOTIFY_MORNING],
    ["to_notify_evening", State.WAIT, State.NOTIFY_EVENING],
    ["to_notify_forgot", State.WAIT, State.NOTIFY_FORGOT],
    ["to_status", State.WAIT, State.STATUS]
]


class TZ(BaseModel):
    name: str

    def __str__(self):
        return f"UTC {datetime.datetime.now(self.timezone).strftime('%z')}"

    @property
    def timezone(self):
        return pytz.timezone(self.name)

    @classmethod
    def from_city(cls, name: str):
        geo_response = geocoder.osm(name)
        logger.info(f'geo_response = {geo_response.__dict__}')
        tz_name = timezone_finder.timezone_at(lng=geo_response.osm['x'], lat=geo_response.osm['y'])
        if not tz_name:
            raise ValueError(f'invalid city name {name}')

        return TZ(name=tz_name)


class Reminder(BaseModel):
    hour: int
    minutes: int
    next_time_ts: float
    tz: TZ

    def format(self, tz):
        n = datetime.datetime.now(self.tz.timezone)
        localized = n.replace(hour=self.hour, minute=self.minutes).astimezone(tz.timezone)
        return f'{localized.hour:02d}:{localized.minute:02d}'

    def __str__(self):
        return f'{self.hour:02d}:{self.minutes:02d}'

    def update_next_time(self):
        if not self.is_notify_needed():
            raise ValueError('trying to update reminder earlier then current next time happened')
        self.next_time_ts = (
                datetime.datetime.fromtimestamp(self.next_time_ts) + datetime.timedelta(days=1)).timestamp()

    def is_notify_needed(self):
        return self.next_time_ts <= datetime.datetime.now().timestamp()

    @classmethod
    def _get_next_time(cls, hour, minutes, tz):
        n = datetime.datetime.now(tz.timezone)
        modified = n.replace(hour=hour, minute=minutes, second=0, microsecond=0)
        if modified > n:
            return modified
        else:
            return modified + datetime.timedelta(days=1)

    def _get_next_time_ts(self) -> float:
        return self._get_next_time(self.hour, self.minutes, self.tz).timestamp()

    @classmethod
    def new(cls, data: str, tz: TZ):
        raw_hour, raw_minutes = data.split(":")

        hour = cls._parse(raw_hour)
        minutes = cls._parse(raw_minutes)

        if hour < 0 or hour > 24:
            raise ValueError("hour must be between 0 and 24")
        if minutes < 0 or minutes > 60:
            raise ValueError("minutes must be between 0 and 60")

        obj = cls(hour=hour, minutes=minutes, next_time_ts=0, tz=tz)
        obj.next_time_ts = obj._get_next_time_ts()
        return obj

    @classmethod
    def _parse(cls, r: str):
        if len(r) > 2 or len(r) < 1:
            raise ValueError(f"invalid length of \"{r}\"")

        if len(r) == 2:
            if r[0] == 0:
                return int(r[1])
        return int(r)


_FORGOT_DELTA = datetime.timedelta(minutes=60)


class ReminderForgot(BaseModel):
    latest_sent_ts: Optional[float]

    @classmethod
    def new(cls):
        return ReminderForgot(latest_sent_ts=None)

    def is_notify_needed(self):
        if not self.latest_sent_ts:
            return False
        normalized_ts = datetime.datetime.fromtimestamp(self.latest_sent_ts).replace(second=0, microsecond=0)
        return normalized_ts + _FORGOT_DELTA <= datetime.datetime.now()

    def update(self):
        self.latest_sent_ts = datetime.datetime.now().timestamp()

    def reset(self):
        self.latest_sent_ts = None


class Measurement(BaseModel):
    high: int
    low: int
    ts: float

    def format(self, tz):
        dt = datetime.datetime.fromtimestamp(self.ts)
        dt_utc = pytz.utc.localize(dt)
        return f'{self.high} на {self.low} {dt_utc.astimezone(tz.timezone).strftime("%Y-%m-%d %H:%M")}'

    @classmethod
    def from_string(cls, data: str, timestamp: float):
        high, low = data.split("/")
        return cls(high=cls._parse(high), low=cls._parse(low), ts=timestamp)

    @classmethod
    def _parse(cls, data: str) -> int:
        value = int(data)
        if value < 0 or value > 1000:
            raise ValueError(f'value must be in range (0,1000) but got {value}')
        return value


class User(BaseModel):
    state: State
    chat_id: int
    reminder_morning: Optional[Reminder]
    reminder_evening: Optional[Reminder]
    reminder_forgot: Optional[ReminderForgot]
    measurements: List[Measurement]
    tz: Optional[TZ]

    @classmethod
    def new(cls, chat_id: str):
        return cls(
            state=State.INITIAL,
            chat_id=chat_id,
            reminder_morning=None,
            reminder_evening=None,
            reminder_forgot=ReminderForgot.new(),
            measurements=[],
            tz=None,
        )


class Messages:
    def __init__(self, user):
        self.user = user

    @property
    def starting(self):
        return """🙌 Здравствуйте! 
Я буду вести дневник давления и напоминать про измерения утром и вечером.
Добавить напоминания можно командой /when, а остановить — командой /stop.
Чтобы записать измерение — отправьте сообщение в формате "120/70".
При первой установке напоминаний я попрошу установить часовой пояс. Позже его можно поменять командой /where
"""

    @property
    def wait_for_tz(self):
        return """🗺 Чтобы правильно установить время, скажите — где вы живёте?\nНужно отправить название города"""

    @property
    def wait_for_morning_time(self):
        return """☀️ Во сколько напоминать утром?\nВведите время в формате ЧЧ:ММ"""

    @property
    def wait_for_evening_time(self):
        return """🌇 Во сколько напоминать вечером?\nФормат такой же — ЧЧ:ММ"""

    @property
    def thanks(self):
        return """🙏 Спасибо"""

    @property
    def thanks_record(self):
        return "📝 Спасибо, записал"

    @property
    def stopped(self):
        return "🛑 Все напоминалки остановлены. Чтобы установить ещё раз — отправьте /when"

    @property
    def reminders(self):
        return f"Напоминалки установлены\n" \
               f"☀️ Утром в {self.user.reminder_morning.format(self.user.tz)}\n" \
               f"🌇 Вечером в {self.user.reminder_evening.format(self.user.tz)}\n" \
               f"{self.tz}"

    @property
    def history(self):
        if self.user.measurements:
            sep = '\n • '
            measurements = sep + sep.join([x.format(self.user.tz) for x in self.user.measurements[::-1]])
            return f'📊 Результаты измерений (часовой пояс {self.user.tz}):' + measurements
        else:
            return '📭 Пока что нет ни одного сохранённого измерения'

    @property
    def notify_morning(self):
        return f'☀️ Доброе утро! Напоминаю про измерение давления!'

    @property
    def notify_evening(self):
        return f'🌇 Добрый вечер! Напоминаю про измерение давления!'

    @property
    def notify_forgot(self):
        return f'🤔 Кажется, вы забыли отправить запись.\n' \
               f'Напоминаю, чтобы записать измерение — отправьте сообщение в формате "120/70"'

    @property
    def tz(self):
        return f'Часовой пояс — {self.user.tz}'


class UserDispatcher(Machine):
    def __init__(self, user: User, message: telebot.types.Message, bot: telebot.TeleBot):
        self.user = user
        self.message = message
        self.bot = bot
        super().__init__(states=State, transitions=transitions, initial=user.state)

    @property
    def messages(self):
        return Messages(self.user)

    def send(self, *messages):
        return self.bot.send_message(self.user.chat_id, '\n'.join(messages))

    def transit(self, state: State):
        logger.debug(f"transition to {state}")
        trigger = "to_" + state.value
        self.dispatch(trigger)
        self.user.state = self.state

    def on_enter_START(self):
        self.send(self.messages.starting)
        self.transit(State.WAIT)

    def on_enter_STOP(self):
        self.user.reminder_morning = None
        self.user.reminder_evening = None
        self.send(self.messages.stopped)

    def on_enter_HISTORY(self):
        self.send(self.messages.history)
        self.transit(State.WAIT)

    def on_enter_RECORD(self):
        measure = Measurement.from_string(self.message.text, timestamp=datetime.datetime.utcnow().timestamp())
        self.user.measurements.append(measure)
        self.user.reminder_forgot.reset()
        self.send(self.messages.thanks_record)
        self.transit(State.WAIT)

    def on_enter_STATUS(self):
        self.send(self.messages.reminders)
        self.transit(State.WAIT)

    def on_enter_NOTIFY_MORNING(self):
        self.user.reminder_morning.update_next_time()
        self.send(self.messages.notify_morning)
        self.user.reminder_forgot.update()
        self.transit(State.WAIT)

    def on_enter_NOTIFY_EVENING(self):
        self.user.reminder_evening.update_next_time()
        self.send(self.messages.notify_evening)
        self.user.reminder_forgot.update()
        self.transit(State.WAIT)

    def on_enter_NOTIFY_FORGOT(self):
        self.send(self.messages.notify_forgot)
        self.user.reminder_forgot.reset()
        self.transit(State.WAIT)

    def on_enter_WAIT_FOR_MORNING_TIME(self):
        self.send(self.messages.wait_for_morning_time)

    def on_exit_WAIT_FOR_MORNING_TIME(self):
        self.user.reminder_morning = Reminder.new(self.message.text, self.user.tz)

    def on_enter_WAIT_FOR_EVENING_TIME(self):
        self.send(self.messages.wait_for_evening_time)

    def on_exit_WAIT_FOR_EVENING_TIME(self):
        self.user.reminder_evening = Reminder.new(self.message.text, self.user.tz)
        self.user.reminder_forgot = ReminderForgot.new()
        self.send(self.messages.thanks, self.messages.reminders)

    def on_enter_WAIT_FOR_TZ(self):
        self.send(self.messages.wait_for_tz)

    def on_exit_WAIT_FOR_TZ(self):
        self.user.tz = TZ.from_city(self.message.text)
        self.send(self.messages.thanks, self.messages.tz)


@contextmanager
def error_handler(chat_id):
    try:
        yield
    except MachineError:
        logger.exception(f'exception when handling message from {chat_id}')
        bot.send_message(chat_id, "Неправильное состояние. Проверьте сообщение и попробуйте ещё раз")
    except Exception:
        logger.exception(f'exception when handling message from {chat_id}')
        bot.send_message(chat_id, "Кажется, здесь какая-то ошибка, давайте попробуем ещё раз")


def get_user_query(chat_id) -> Query:
    return Query().chat_id == chat_id  # noqa


def _get_user(message: telebot.types.Message):
    result = db.table("users").search(get_user_query(message.chat.id))
    if not result:
        return None
    return User(**result[0])


def _check_state(message: telebot.types.Message, state: State) -> bool:
    user = _get_user(message)
    return user and user.state == state


def handle(message, state):
    chat_id = message.chat.id

    with error_handler(chat_id=chat_id):
        result = db.table("users").search(get_user_query(chat_id))

        if result:
            user = User(**result[0])
        else:
            user = User.new(chat_id)

        UserDispatcher(
            user,
            message,
            bot
        ).transit(state)

        db.table("users").upsert(user.dict(), get_user_query(chat_id))


@bot.message_handler(commands=['старт', 'start'])
def handle_start(message: telebot.types.Message):
    handle(message, State.START)


@bot.message_handler(commands=['стоп', 'stop'])
def handle_stop(message: telebot.types.Message):
    handle(message, State.STOP)


@bot.message_handler(commands=['история', 'history'])
def handle_history(message: telebot.types.Message):
    handle(message, State.HISTORY)


@bot.message_handler(commands=['когда', 'when'])
def handle_when(message: telebot.types.Message):
    if _get_user(message).tz is None:
        # flow of setting reminders for first time
        handle(message, State.WAIT_FOR_TZ)
    else:
        handle(message, State.WAIT_FOR_MORNING_TIME)


@bot.message_handler(commands=['где', 'where'])
def handle_where(message: telebot.types.Message):
    handle(message, State.WAIT_FOR_TZ)


@bot.message_handler(commands=['статус', 'status'])
def handle_status(message: telebot.types.Message):
    handle(message, State.STATUS)


@bot.message_handler(regexp=r'\d\d\:\d\d', func=lambda msg: _check_state(msg, State.WAIT_FOR_MORNING_TIME))
def handle_time_morning(message: telebot.types.Message):
    handle(message, State.WAIT_FOR_EVENING_TIME)


@bot.message_handler(regexp=r'\d\d\:\d\d', func=lambda msg: _check_state(msg, State.WAIT_FOR_EVENING_TIME))
def handle_time_evening(message: telebot.types.Message):
    handle(message, State.WAIT)


@bot.message_handler(regexp=r'\d{1,3}\/\d{1,3}', func=lambda msg: _check_state(msg, State.WAIT))
def handle_record(message: telebot.types.Message):
    handle(message, State.RECORD)


@bot.message_handler(func=lambda msg: _check_state(msg, State.WAIT_FOR_TZ))
def handle_message(message: telebot.types.Message):
    if _get_user(message).tz is None:
        # flow of setting reminders for first time
        handle(message, State.WAIT_FOR_MORNING_TIME)
    else:
        handle(message, State.WAIT)


@bot.message_handler()
def handle_message(message: telebot.types.Message):
    bot.send_message(message.chat.id, f'🤔')


def worker():
    start_time = datetime.datetime.now().replace(second=0, microsecond=0).timestamp()

    while True:
        logger.info(f'starting worker cycle')
        for user in [User(**x) for x in db.table("users").all()]:
            with error_handler(chat_id=user.chat_id):
                if user.reminder_morning and user.reminder_morning.is_notify_needed():
                    logger.info(f'notifying morning {user}')
                    UserDispatcher(user, None, bot).transit(State.NOTIFY_MORNING)
                    db.table("users").upsert(user.dict(), get_user_query(user.chat_id))

                if user.reminder_evening and user.reminder_evening.is_notify_needed():
                    logger.info(f'notifying evening {user}')
                    UserDispatcher(user, None, bot).transit(State.NOTIFY_EVENING)
                    db.table("users").upsert(user.dict(), get_user_query(user.chat_id))

                if user.reminder_forgot and user.reminder_forgot.is_notify_needed():
                    logger.info(f'notify forgot {user}')
                    UserDispatcher(user, None, bot).transit(State.NOTIFY_FORGOT)
                    db.table("users").upsert(user.dict(), get_user_query(user.chat_id))

        time.sleep(60.0 - ((time.time() - start_time) % 60.0))


if __name__ == "__main__":
    Thread(target=worker, daemon=True).start()
    bot.polling()
