import datetime
import enum
import os
import time
from contextlib import contextmanager
from threading import Thread
from typing import Optional

import telebot
from loguru import logger
from tinydb import TinyDB, Query
from tinydb.storages import MemoryStorage
from transitions import Machine

token = os.getenv("TELEGRAM_BOT_TOKEN")

bot = telebot.TeleBot(token)

db_path = os.getenv("DB_PATH")
if db_path:
    logger.info(f'init db on path={db_path}')
    db = TinyDB(db_path)
else:
    logger.info('init in-memory db')
    db = TinyDB(storage=MemoryStorage)


class State(enum.Enum):
    INITIAL = "initial"

    HISTORY = "history"

    NOTIFY = "notify"

    START = "start"
    STARTING = "starting"

    WHEN = "when"
    WHEN_WAIT_FOR_TIME = "when_wait_for_time"
    WHEN_GOT_TIME = "when_got_time"

    WAIT = "wait"

    # RECORD = "record"
    # RECORD_WAIT_FOR_PRESSURE = "record_wait_for_pressure"
    # RECORD_WAIT_FOR_TIME = "record_wait_for_time"
    # RECORD_WAIT_FOR_DATE = "record_wait_for_date"
    RECORD_LITE = "record_lite"

    STOPPED = "stopped"


transitions = [
    ["to_starting", State.START, State.STARTING],
    ["to_wait", [State.STARTING, State.WHEN_GOT_TIME, State.RECORD_LITE, State.START, State.HISTORY, State.NOTIFY],
     State.WAIT],
    ["to_when", [State.STOPPED, State.WAIT], State.WHEN],
    ["to_when_wait_for_time", State.WHEN, State.WHEN_WAIT_FOR_TIME],
    ["to_when_got_time", State.WHEN_WAIT_FOR_TIME, State.WHEN_GOT_TIME],
    # ["to_record", State.WAIT, State.RECORD],
    # ["to_record_wait_for_pressure", State.RECORD, State.RECORD_WAIT_FOR_PRESSURE],
    # ["to_record_wait_for_time", State.RECORD_WAIT_FOR_PRESSURE, State.RECORD_WAIT_FOR_TIME],
    # ["to_record_wait_for_date", State.RECORD_WAIT_FOR_TIME, State.RECORD_WAIT_FOR_DATE],
    ["to_record_lite", State.WAIT, State.RECORD_LITE],
    ["to_stopped", '*', State.STOPPED],
    ["to_start", '*', State.START],
    ["to_history", State.WAIT, State.HISTORY],
    ["to_notify", State.WAIT, State.NOTIFY]
]

text_start = """Привет!
Я могу напоминать про измерение давления и сохранять результаты измерений.
Сейчас я буду присылать напоминалки в {reminder}.

Изменить время напоминалок или часовой пояс можно командой "/когда".

Чтобы записать измерение нужно просто отправить сообщение в формате "120/40"

А чтобы остановить напоминания — просто отправь «стоп»
"""

text_already_start = """Бот уже запущен и напоминалка установлена на {reminder}"""

text_when = """Давайте договоримся о том, когда мы будем общаться. Введите время в формате ЧЧ:ММ по Москве"""

text_when_reminder_set = "Спасибо! Теперь я буду напоминать каждый день в {reminder}"

text_stopped = "Напоминалка остановлена. Чтобы установить ещё раз — отправь /когда"


class Reminder:
    def __init__(self, hour, minutes, next_time):
        self.hour = hour
        self.minutes = minutes
        self.next_time = next_time

    def __str__(self):
        return f'{self.hour} часов и {self.minutes} минут по Москве (UTC +3)'

    @classmethod
    def from_json(cls, data: dict):
        return cls(data['hour'], data['minutes'], datetime.datetime.fromtimestamp(data['next_time']))

    def to_json(self):
        return {
            'hour': self.hour,
            'minutes': self.minutes,
            'next_time': self.next_time.timestamp(),
        }

    @classmethod
    def _get_next_time(cls, hour, minutes):
        n = datetime.datetime.now()
        modified = n.replace(hour=hour, minute=minutes, second=0, microsecond=0)
        if modified > n:
            return modified
        else:
            return modified + datetime.timedelta(days=1)

    def update_next_time(self):
        self.next_time = self.next_time + datetime.timedelta(days=1)

    @classmethod
    def new(cls, data: str):
        raw_hour, raw_minutes = data.split(":")

        hour = cls._parse(raw_hour)
        minutes = cls._parse(raw_minutes)

        if hour < 0 or hour > 24:
            raise ValueError("hour must be between 0 and 24")
        if minutes < 0 or minutes > 60:
            raise ValueError("minutes must be between 0 and 60")

        return cls(hour, minutes, cls._get_next_time(hour, minutes))

    @classmethod
    def _parse(cls, r: str):
        if len(r) > 2 or len(r) < 1:
            raise ValueError(f"invalid length of \"{r}\"")

        if len(r) == 2:
            if r[0] == 0:
                return int(r[1])
        return int(r)

    def is_notify_needed(self):
        return self.next_time <= datetime.datetime.now()


class Measure:
    def __init__(self, high, low, timestamp):
        self.high: int = high
        self.low: int = low
        self.timestamp: float = timestamp

    def __str__(self):
        return f'{datetime.datetime.fromtimestamp(self.timestamp).strftime("%Y-%m-%d %H:%M")} — {self.high} на {self.low}'

    @classmethod
    def from_string(cls, data: str, timestamp: float):
        high, low = data.split("/")
        return cls(cls._parse(high), cls._parse(low), timestamp)

    @classmethod
    def from_json(cls, data: dict):
        return cls(data['high'], data['low'], data['timestamp'])

    def to_json(self):
        return {
            'high': self.high,
            'low': self.low,
            'timestamp': self.timestamp
        }

    @classmethod
    def _parse(cls, data: str) -> int:
        value = int(data)
        if value < 0 or value > 1000:
            raise ValueError(f'value must be in range (0,1000) but got {value}')
        return value


class User(Machine):
    def __init__(self, chat_id, state: State, reminder: Optional[Reminder], measurements: list):
        self.chat_id = chat_id
        self.reminder = reminder
        self.measurements = measurements
        super().__init__(states=State, transitions=transitions, initial=state)

    @classmethod
    def new(cls, chat_id: str):
        return User(chat_id, State.INITIAL, None, [])

    @classmethod
    def from_json(cls, data: dict):
        return cls(
            data['chat_id'],
            State(data['state']),
            Reminder.from_json(data['reminder']) if data['reminder'] else None,
            [Measure.from_json(x) for x in data['measurements']]
        )

    def to_json(self):
        return {
            'chat_id': self.chat_id,
            'state': self.state.value,
            'reminder': self.reminder.to_json() if self.reminder else None,
            'measurements': [x.to_json() for x in self.measurements]
        }

    def transit(self, state: State, message: telebot.types.Message):
        logger.debug(f"transition to {state}")
        trigger = "to_" + state.value
        self.dispatch(trigger, message=message)  # noqa

    def on_enter_START(self, message):
        logger.info(f"handling start() method")
        if self.reminder is not None:
            bot.send_message(self.chat_id, text_already_start.format(reminder=self.reminder))
            self.transit(State.WAIT, message=message)
            return

        self.transit(State.STARTING, message=message)

    def on_enter_STARTING(self, message):
        logger.info(f'sending start message for {self.chat_id}')
        self.reminder = Reminder.new("09:00")
        bot.send_message(self.chat_id, text_start.format(reminder=self.reminder))
        self.transit(State.WAIT, message)

    def on_enter_WHEN(self, message):
        logger.info(f'sending message about when for {self.chat_id}')
        markup = telebot.types.ForceReply(selective=False)
        bot.send_message(self.chat_id, text_when, reply_markup=markup)
        self.transit(State.WHEN_WAIT_FOR_TIME, message)

    def on_enter_WHEN_GOT_TIME(self, message: telebot.types.Message):
        self.reminder = Reminder.new(message.text)
        bot.send_message(self.chat_id, text_when_reminder_set.format(reminder=self.reminder), reply_markup=None)
        self.transit(State.WAIT, message)

    def on_enter_RECORD_LITE(self, message: telebot.types.Message):
        logger.info(f'received data for record: {message.text}')
        measure = Measure.from_string(message.text, timestamp=time.time())
        self.measurements.append(measure)
        bot.reply_to(message, f"Спасибо! Сохранил: {measure}")
        self.transit(State.WAIT, message)

    def on_enter_STOPPED(self, message: telebot.types.Message):
        self.reminder = None
        bot.send_message(self.chat_id, text_stopped)

    def on_enter_HISTORY(self, message: telebot.types.Message):
        if self.measurements:
            bot.send_message(self.chat_id,
                             'Результаты измерений:\n - ' + '\n - '.join([str(x) for x in self.measurements[::-1]]))
        else:
            bot.send_message(self.chat_id, 'Пока что нет ни одного сохранённого измерения')

        self.transit(State.WAIT, message)

    def on_enter_NOTIFY(self, message: telebot.types.Message):
        self.reminder.update_next_time()
        bot.send_message(self.chat_id,
                         f'Напоминаю про измерение давления! Следующий раз напоминание будет {self.reminder.next_time}')
        self.transit(State.WAIT, message)


@contextmanager
def error_handler(chat_id):
    try:
        yield
    except Exception:
        logger.exception(f'exception when handling message from {chat_id}')
        bot.send_message(chat_id, "Кажется, здесь какая-то ошибка, давайте попробуем еще раз")


def get_user_query(chat_id):
    return Query().chat_id == chat_id


def handle(message, state):
    chat_id = message.chat.id

    with error_handler(chat_id=chat_id):
        result = db.table("users").search(get_user_query(chat_id))

        if result:
            user = User.from_json(result[0])
        else:
            user = User.new(chat_id)

        user.transit(state, message=message)

        db.table("users").upsert(user.to_json(), get_user_query(chat_id))


@bot.message_handler(commands=['старт', 'start'])
def handle_start(message: telebot.types.Message):
    handle(message, State.START)


@bot.message_handler(commands=['когда', 'when'])
def handle_when(message: telebot.types.Message):
    handle(message, State.WHEN)


@bot.message_handler(commands=['стоп', 'stop'])
def handle_stop(message: telebot.types.Message):
    handle(message, State.STOPPED)


@bot.message_handler(commands=['история', 'history'])
def handle_history(message: telebot.types.Message):
    handle(message, State.HISTORY)


@bot.message_handler(regexp=r'\d\d\:\d\d')
def handle_when_reply(message: telebot.types.Message):
    handle(message, State.WHEN_GOT_TIME)


@bot.message_handler(regexp=r'\d{1,3}\/\d{1,3}')
def handle_record_lite(message: telebot.types.Message):
    handle(message, State.RECORD_LITE)


def worker():
    start_time = datetime.datetime.now().replace(second=0, microsecond=0).timestamp()

    while True:
        logger.info(f'starting worker cycle')
        for user in [User.from_json(x) for x in db.table("users").all()]:
            with error_handler(chat_id=user.chat_id):
                if user.reminder.is_notify_needed():
                    logger.info(f'notifying {user}')
                    user.transit(State.NOTIFY, None)
                    db.table("users").upsert(user.to_json(), get_user_query(user.chat_id))

        time.sleep(60.0 - ((time.time() - start_time) % 60.0))


if __name__ == "__main__":
    Thread(target=worker, daemon=True).start()
    bot.polling()
