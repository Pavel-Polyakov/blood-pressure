import datetime
import enum
from typing import List, Optional

import geocoder
import pytz
from loguru import logger
from pydantic import BaseModel
from timezonefinder import TimezoneFinder


class State(str, enum.Enum):
    INITIAL = "initial"
    HISTORY = "history"
    START = "start"
    NOTIFY_MORNING = "notify_morning"
    NOTIFY_EVENING = "notify_evening"
    NOTIFY_FORGOT = "notify_forgot"
    WAIT_FOR_MORNING_TIME = "wait_for_morning_time"
    WAIT_FOR_EVENING_TIME = "wait_for_evening_time"
    WAIT_FOR_TZ_WHEN = "wait_for_tz_when"
    WAIT_FOR_TZ_HISTORY = "wait_for_tz_history"
    WAIT_FOR_TZ = "wait_for_tz"
    WAIT = "wait"
    RECORD = "record"
    STOP = "stop"
    STATUS = "status"


timezone_finder = TimezoneFinder()


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
        return f'{self.high} на {self.low} － ({dt_utc.astimezone(tz.timezone).strftime("%Y-%m-%d %H:%M")})'

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

📝 120/70 — Записать измерение
📆 /when — Добавить напоминания
🛑 /stop — Остановить напоминания
🗺 /where — Установить часовой пояс
📊 /history — История измерений
"""

    @property
    def wait_for_tz(self):
        return """🗺 Чтобы установить часовой пояс, скажите — где вы живёте?\nНужно отправить название города"""

    @property
    def wait_for_tz_when(self):
        return """🗺 Чтобы правильно установить время напоминалок, скажите — где вы живёте?\nНужно отправить название города"""

    @property
    def wait_for_tz_history(self):
        return """🗺 Чтобы правильно отобразить запись измерений, скажите — где вы живёте?\nНужно отправить название города"""

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
        return "🛑 Все напоминалки остановлены. Чтобы установить — отправьте /when"

    @property
    def reminders(self):
        if not self.user.reminder_morning or not self.user.reminder_evening:
            return self.stopped

        return f"Напоминалки установлены\n" \
               f"☀️ Утром в {self.user.reminder_morning.format(self.user.tz)}\n" \
               f"🌇 Вечером в {self.user.reminder_evening.format(self.user.tz)}\n" \
               f"{self.tz}"

    @property
    def history(self):
        if self.user.measurements:
            tz = self.user.tz
            sep = '\n • '
            measurements = sep + sep.join([x.format(tz) for x in self.user.measurements[::-1]])
            return f'📊 Результаты измерений ({self.tz}):' + measurements
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
